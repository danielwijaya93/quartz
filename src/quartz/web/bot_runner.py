"""Background watcher, GitHub poller, cron matcher, and worker pool for Quartz Bots."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quartz.bots import BotConfig, BotHistoryStore, BotQueue, BotRunRecord, BotStore, QueuedTrigger
from quartz.core.turn import TurnStatus

logger = logging.getLogger(__name__)

TriggerCallback = Callable[[BotConfig, QueuedTrigger], Awaitable[TurnStatus | str | None]]

_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 7),   # day of week (0 and 7 both represent Sunday)
)


def _parse_cron_field(field: str, min_val: int, max_val: int, *, is_dow: bool = False) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        if not part:
            raise ValueError(f"Empty cron token in field {field!r}")
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"Cron step must be > 0 in {part!r}")
        else:
            base, step = part, 1

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            start_str, end_str = base.split("-", 1)
            start, end = int(start_str), int(end_str)
            if start > end or start < min_val or end > max_val:
                raise ValueError(f"Cron range out of bounds in {part!r}")
        else:
            if "/" in part:
                raise ValueError(f"Invalid stepped cron token {part!r}")
            val = int(base)
            if val < min_val or val > max_val:
                raise ValueError(f"Cron value out of bounds in {part!r}")
            start, end = val, val

        for num in range(start, end + 1, step):
            values.add(0 if (is_dow and num == 7) else num)
    return values


def cron_matches(expression: str, dt: datetime) -> bool:
    """Return True if a 5-field cron expression matches ``dt``."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression (expected 5 fields): {expression!r}")

    # Convert Python weekday (Mon=0..Sun=6) to cron weekday (Sun=0..Sat=6).
    cron_dow = (dt.weekday() + 1) % 7
    targets = (dt.minute, dt.hour, dt.day, dt.month, cron_dow)

    for idx, (field, (min_v, max_v)) in enumerate(zip(fields, _FIELD_RANGES, strict=True)):
        allowed = _parse_cron_field(field, min_v, max_v, is_dow=(idx == 4))
        if targets[idx] not in allowed:
            return False
    return True


async def _default_run_trigger(bot: BotConfig, trigger: QueuedTrigger) -> TurnStatus:
    logger.info("Bot %s triggered (%s: %s)", bot.id, trigger.source, trigger.event_key)
    return TurnStatus.OK


class BotRunner:
    """Coordinates cron/GitHub watchers and executes queued triggers via a worker pool."""

    def __init__(
        self,
        store: BotStore,
        queue: BotQueue,
        run_trigger: TriggerCallback | None = None,
        *,
        history: BotHistoryStore | None = None,
        max_concurrent: int = 2,
        tick_interval_s: float = 15.0,
        gh_cmd: str = "gh",
    ) -> None:
        self._store = store
        self._queue = queue
        self._history = history
        self._run_trigger: TriggerCallback = run_trigger or _default_run_trigger
        self._max_concurrent = max(1, int(max_concurrent))
        self._tick_interval_s = max(0.05, float(tick_interval_s))
        self._gh_cmd = gh_cmd

        self._active_slots = 0
        self._last_cron_minute: dict[str, tuple[int, int, int, int, int]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._worker_tasks: set[asyncio.Task[None]] = set()
        self._wake_event = asyncio.Event()
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_count(self) -> int:
        return self._active_slots

    def notify_queue(self) -> None:
        """Wake the runner loop immediately when new items are enqueued."""
        self._wake_event.set()

    def start(self) -> None:
        if self.running:
            return
        self._stopped = False
        self._wake_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="quartz-bot-runner")

    async def stop(self) -> None:
        self._stopped = True
        self._wake_event.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for task in list(self._worker_tasks):
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()

    async def _run_loop(self) -> None:
        while not self._stopped:
            try:
                await self.tick_once()
                await self.dispatch_available()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("BotRunner loop tick failed", exc_info=True)

            self._wake_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._tick_interval_s)

    async def tick_once(self, now: datetime | None = None) -> None:
        """Evaluate all enabled cron and GitHub poll watchers once."""
        current = now or datetime.now(UTC)
        bots = await asyncio.to_thread(self._store.list_bots)
        for bot in bots:
            if not bot.enabled:
                continue
            if bot.watcher_type == "cron":
                await self._tick_cron(bot, current)
            elif bot.watcher_type == "github_poll":
                await self._tick_github_poll(bot, current.timestamp())

    async def _tick_cron(self, bot: BotConfig, now_utc: datetime) -> None:
        if bot.cron is None:
            return
        try:
            tz = ZoneInfo(bot.cron.timezone)
        except Exception:
            logger.warning("Invalid timezone %r for bot %s", bot.cron.timezone, bot.id)
            return
        local_dt = now_utc.astimezone(tz)
        minute_key = (local_dt.year, local_dt.month, local_dt.day, local_dt.hour, local_dt.minute)
        if self._last_cron_minute.get(bot.id) == minute_key:
            return
        try:
            matched = cron_matches(bot.cron.expression, local_dt)
        except ValueError:
            logger.warning("Invalid cron expression for bot %s: %r", bot.id, bot.cron.expression)
            return
        if not matched:
            return

        self._last_cron_minute[bot.id] = minute_key
        event_key = f"cron:{local_dt.strftime('%Y-%m-%dT%H:%M')}"
        trigger = QueuedTrigger(
            trigger_id=uuid.uuid4().hex,
            bot_id=bot.id,
            event_key=event_key,
            source="cron",
            payload={"scheduled_at": local_dt.isoformat()},
            enqueued_at=now_utc.timestamp(),
        )
        await asyncio.to_thread(self._queue.enqueue, trigger, bot.max_queue_depth)

    async def _tick_github_poll(self, bot: BotConfig, now_ts: float) -> None:
        if bot.github is None:
            return
        state = await asyncio.to_thread(self._store.get_state, bot.id)
        if state.last_polled_at is not None and (now_ts - state.last_polled_at) < bot.github.interval_seconds:
            return

        repo = bot.github.repo
        endpoints: list[tuple[str, str]] = []
        for ev in bot.github.events:
            if ev == "issues":
                endpoints.append(("issue", f"repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=20"))
            elif ev in ("pull_request", "pulls"):
                endpoints.append(("pr", f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=20"))

        last_error: str | None = None
        for kind, endpoint in endpoints:
            items, err = await self._run_gh_api(endpoint)
            if err is not None:
                last_error = err
                continue
            for item in items:
                if kind == "issue" and "pull_request" in item:
                    continue
                number = item.get("number") or item.get("id")
                if number is None:
                    continue
                event_key = f"gh:{kind}:{repo}#{number}"
                newly_seen = await asyncio.to_thread(self._store.mark_seen, bot.id, event_key)
                if not newly_seen:
                    continue
                trigger = QueuedTrigger(
                    trigger_id=uuid.uuid4().hex,
                    bot_id=bot.id,
                    event_key=event_key,
                    source="github_poll",
                    payload=item,
                    enqueued_at=now_ts,
                )
                await asyncio.to_thread(self._queue.enqueue, trigger, bot.max_queue_depth)

        fresh_state = await asyncio.to_thread(self._store.get_state, bot.id)
        fresh_state.last_polled_at = now_ts
        fresh_state.last_error = last_error
        await asyncio.to_thread(self._store.save_state, fresh_state)

    async def _run_gh_api(self, endpoint: str) -> tuple[list[dict[str, Any]], str | None]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._gh_cmd,
                "api",
                endpoint,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except OSError as exc:
            return [], f"gh api failed: {exc}"

        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip() or f"exit code {proc.returncode}"
            return [], f"gh api error: {msg}"

        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            return [], f"gh api returned invalid JSON: {exc}"

        if not isinstance(parsed, list):
            return [], "gh api returned non-list payload"
        return [x for x in parsed if isinstance(x, dict)], None

    async def dispatch_available(self) -> int:
        """Dequeue and start worker tasks up to ``max_concurrent``."""
        started = 0
        while self._active_slots < self._max_concurrent:
            trigger = await asyncio.to_thread(self._queue.dequeue_next)
            if trigger is None:
                break
            bot = await asyncio.to_thread(self._store.get_bot, trigger.bot_id)
            if bot is None or not bot.enabled:
                continue

            self._active_slots += 1
            started += 1
            task = asyncio.create_task(
                self._execute_trigger(bot, trigger),
                name=f"bot-worker-{bot.id}-{trigger.trigger_id[:8]}",
            )
            self._worker_tasks.add(task)
            task.add_done_callback(self._worker_tasks.discard)
        return started

    async def _execute_trigger(self, bot: BotConfig, trigger: QueuedTrigger) -> None:
        slot_released = False
        run_id = uuid.uuid4().hex
        if self._history is not None:
            await asyncio.to_thread(
                self._history.record_run_start,
                BotRunRecord(
                    run_id=run_id,
                    bot_id=bot.id,
                    bot_name=bot.name,
                    trigger_id=trigger.trigger_id,
                    event_key=trigger.event_key,
                    source=trigger.source,
                    status="running",
                    started_at=time.time(),
                ),
            )
        try:
            outcome = await self._run_trigger(bot, trigger)
            run_status = "ok"
            if outcome == TurnStatus.WAITING_PERMISSION or outcome == TurnStatus.WAITING_PERMISSION.value:
                run_status = "waiting_permission"
                self._active_slots = max(0, self._active_slots - 1)
                slot_released = True
                self._wake_event.set()
            elif outcome == TurnStatus.ERROR or outcome == TurnStatus.ERROR.value:
                run_status = "error"
            elif outcome in (
                TurnStatus.INTERRUPTED,
                TurnStatus.INTERRUPTED.value,
                TurnStatus.ABORTED,
                TurnStatus.ABORTED.value,
            ):
                run_status = "cancelled"

            finished_at = time.time()
            state = await asyncio.to_thread(self._store.get_state, bot.id)
            state.last_run_at = finished_at
            state.last_error = None
            await asyncio.to_thread(self._store.save_state, state)
            if self._history is not None:
                await asyncio.to_thread(
                    self._history.record_run_finish,
                    run_id,
                    status=run_status,
                    finished_at=finished_at,
                    session_id=state.active_session_id,
                )
        except asyncio.CancelledError:
            if self._history is not None:
                with contextlib.suppress(Exception):
                    state = await asyncio.to_thread(self._store.get_state, bot.id)
                    await asyncio.to_thread(
                        self._history.record_run_finish,
                        run_id,
                        status="cancelled",
                        finished_at=time.time(),
                        session_id=state.active_session_id,
                    )
            raise
        except Exception as exc:
            logger.warning("Bot %s trigger %s failed", bot.id, trigger.trigger_id, exc_info=True)
            finished_at = time.time()
            state = await asyncio.to_thread(self._store.get_state, bot.id)
            state.last_run_at = finished_at
            state.last_error = str(exc)
            await asyncio.to_thread(self._store.save_state, state)
            if self._history is not None:
                await asyncio.to_thread(
                    self._history.record_run_finish,
                    run_id,
                    status="error",
                    finished_at=finished_at,
                    session_id=state.active_session_id,
                    error=str(exc),
                )
        finally:
            if not slot_released:
                self._active_slots = max(0, self._active_slots - 1)
                self._wake_event.set()
