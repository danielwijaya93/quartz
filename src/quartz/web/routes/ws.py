"""WebSocket `/ws` route and frame dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from quartz.web.auth import WS_AUTH_CLOSE_CODE, extract_ws_token, verify_token
from quartz.web.context import (
    MAX_FRAME_BYTES,
    AlreadyReported,
    AppContext,
    attachments_from_raw,
    command_payload,
    is_allowed_origin,
    remember_workspace,
)
from quartz.web.schemas import (
    CLIENT_FRAME_TYPES,
    ActiveSessionDismissFrame,
    ActiveSessionsClearReadFrame,
    CancelFrame,
    CommandFrame,
    HelloFrame,
    LoadSessionFrame,
    NewSessionFrame,
    NotificationsReadFrame,
    PermissionResponseFrame,
    PromptFrame,
    SetSandboxFrame,
    SteerFrame,
    TrustProjectFrame,
    WatcherActionFrame,
    WatcherConfigureFrame,
    parse_client_frame,
)
from quartz.web.session import SessionManager, TurnBusyError, WebSession, resolve_cwd
from quartz.web.sidebar import SidebarStore, default_chats_dir


class _ConnState:
    """Per-WebSocket active session pointer."""

    def __init__(self) -> None:
        self.session: WebSession | None = None


def _err(push: Callable[[dict[str, Any]], None], message: str, *, fatal: bool = False) -> None:
    push({"type": "error", "message": message, "fatal": fatal})


def _switch_connection(
    conn: _ConnState, target: WebSession, push: Callable[[dict[str, Any]], None], app: FastAPI
) -> None:
    """Move this WebSocket connection to `target` without retiring the old session."""
    if conn.session is not target:
        if conn.session is not None:
            conn.session.detach(push)
        target.attach(push)
        conn.session = target
    app.state.session = target


async def _ensure_conn_session(
    manager: SessionManager,
    conn: _ConnState,
    push: Callable[[dict[str, Any]], None],
    app: FastAPI,
    cwd: Path | None = None,
) -> WebSession:
    if conn.session is not None and conn.session.built:
        return conn.session
    try:
        target = await manager.aensure_default_session(cwd)
    except Exception as exc:
        _err(push, f"{exc.__class__.__name__}: {exc}", fatal=True)
        raise AlreadyReported from exc
    _switch_connection(conn, target, push, app)
    return target


def _replay_live_turn(
    session: WebSession, snapshot: dict[str, Any], push: Callable[[dict[str, Any]], None]
) -> None:
    if not snapshot.get("turnRunning") or session.turn_id is None:
        return
    push({"type": "turn_start", "turnId": session.turn_id})
    if session.turn_status:
        push({"type": "status", "label": session.turn_status})


async def _dispatch(
    manager: SessionManager,
    conn: _ConnState,
    app: FastAPI,
    socket: WebSocket,
    message: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
    sidebar: Callable[[], SidebarStore],
) -> None:
    """Route one client frame. Every failure becomes an `error` frame, never a 500."""
    del socket
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in CLIENT_FRAME_TYPES:
        _err(push, f"Unknown frame type: {kind}")
        return
    try:
        frame = parse_client_frame(message)
    except ValidationError as exc:
        _err(push, f"Invalid frame: {exc}")
        return

    try:
        if isinstance(frame, HelloFrame):
            manager.set_system_prompt_append(frame.system_prompt_append or "")
            session: WebSession | None = None
            if isinstance(frame.session_id, str) and frame.session_id.strip():
                with contextlib.suppress(Exception):
                    session = await manager.aget_or_load_session(frame.session_id.strip())
            if session is None:
                session = await _ensure_conn_session(manager, conn, push, app, resolve_cwd(frame.cwd, manager.cwd))
            else:
                _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            from quartz.settings import load_settings

            loaded, snapshot = await asyncio.gather(
                asyncio.to_thread(load_settings),
                session.asnapshot(),
            )
            snapshot["settings"] = {
                "notifications": loaded.notifications,
                "notifyTurn": loaded.notify_turn,
                "notifyApproval": loaded.notify_approval,
                "notifyBackground": loaded.notify_background,
                "notifyError": loaded.notify_error,
                "richUi": loaded.rich_ui,
            }
            snapshot["watcher"] = manager.watcher.state_payload(session.runtime.active_session.id)
            push(snapshot)
            _replay_live_turn(session, snapshot, push)
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif isinstance(frame, PromptFrame):
            text = frame.text.strip()
            images = attachments_from_raw([img.model_dump(by_alias=True) for img in (frame.images or [])])
            if not text and not images:
                _err(push, "Empty prompt.")
                return
            session = await _ensure_conn_session(manager, conn, push, app)
            if session.has_pending_permission:
                _err(push, "Waiting for permission approval. Allow or reject the pending request first.")
                return
            if session.turn_running:
                _err(push, "A turn is already running. Press Esc to abort it.")
                return
            from quartz.core.event_bus import SessionEvent

            session.bind_loop(asyncio.get_running_loop())
            manager.watcher.bind_loop(asyncio.get_running_loop())
            manager.event_bus.publish(
                SessionEvent(kind="user_message", session_id=session.runtime.active_session.id)
            )
            session.start_turn(text, images)

        elif isinstance(frame, SteerFrame):
            if text := frame.text.strip():
                (await _ensure_conn_session(manager, conn, push, app)).steer(text)

        elif isinstance(frame, CancelFrame):
            (await _ensure_conn_session(manager, conn, push, app)).cancel()

        elif isinstance(frame, CommandFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            before = (session.runtime.active_session.id, session.runtime.model, session.runtime.effort)
            ok, result = await session.run_command_async(frame.name, frame.arg or "")
            push({"type": "command_result", "name": frame.name.lstrip("/"), "ok": ok, "result": command_payload(result)})
            if (session.runtime.active_session.id, session.runtime.model, session.runtime.effort) != before:
                push(session.snapshot())

        elif isinstance(frame, NewSessionFrame):
            if frame.cwd is not None:
                chats_dir = default_chats_dir()
                try:
                    if Path(frame.cwd).expanduser().resolve() == chats_dir:
                        chats_dir.mkdir(parents=True, exist_ok=True)
                except (OSError, RuntimeError, ValueError):
                    pass
            resolved_cwd = None if frame.cwd is None else resolve_cwd(frame.cwd, manager.cwd)
            session = await manager.acreate_session(resolved_cwd)
            _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            snapshot = await session.asnapshot()
            snapshot["watcher"] = manager.watcher.state_payload(session.runtime.active_session.id)
            push(snapshot)
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif isinstance(frame, LoadSessionFrame):
            session_id = frame.session_id.strip()
            if not session_id:
                _err(push, "Missing sessionId.")
                return
            session = await manager.aget_or_load_session(session_id)
            _switch_connection(conn, session, push, app)
            snapshot = await session.asnapshot()
            snapshot["watcher"] = manager.watcher.state_payload(session.runtime.active_session.id)
            push(snapshot)
            _replay_live_turn(session, snapshot, push)
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif isinstance(frame, TrustProjectFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            await session.atrust_current_project()
            push(await session.asnapshot())

        elif isinstance(frame, SetSandboxFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            await session.aset_sandbox(frame.enabled)
            session.emit(await session.asnapshot())

        elif isinstance(frame, PermissionResponseFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            request_id = frame.request_id.strip()
            if not request_id:
                _err(push, "Missing requestId.")
                return
            session.resolve_permission(request_id, frame.decision.strip())

        elif isinstance(frame, ActiveSessionDismissFrame):
            session_id = frame.session_id.strip()
            if not session_id:
                _err(push, "Missing sessionId.")
                return
            manager.dismiss_active_session(session_id)

        elif isinstance(frame, ActiveSessionsClearReadFrame):
            manager.clear_read_active_sessions()

        elif isinstance(frame, NotificationsReadFrame):
            manager.broadcast_notifications_cleared()

        elif isinstance(frame, WatcherConfigureFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            sid = session.runtime.active_session.id
            if frame.mode == "goal" and frame.goal is not None:
                manager.watcher.set_goal(
                    sid,
                    frame.goal,
                    max_turns=frame.max_turns,
                    delay_seconds=float(frame.delay_seconds) if frame.delay_seconds is not None else None,
                    prompt_template=frame.prompt_template,
                )
            else:
                manager.watcher.configure_session(
                    sid,
                    mode=frame.mode,
                    goal=frame.goal,
                    prompt_template=frame.prompt_template,
                    delay_seconds=float(frame.delay_seconds) if frame.delay_seconds is not None else None,
                    max_turns=frame.max_turns,
                )

        elif isinstance(frame, WatcherActionFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            sid = session.runtime.active_session.id
            if frame.action == "continue_now":
                await manager.watcher.continue_now(sid)
            elif frame.action == "cancel_countdown":
                manager.watcher.cancel_countdown(sid)
            elif frame.action == "pause":
                manager.watcher.pause(sid, reason="paused by user")
            elif frame.action == "resume":
                manager.watcher.resume(sid)
            elif frame.action == "clear":
                manager.watcher.clear_goal(sid)

    except WebSocketDisconnect:
        raise
    except AlreadyReported:
        return
    except TurnBusyError as exc:
        _err(push, str(exc))
    except Exception as exc:
        _err(push, f"{exc.__class__.__name__}: {exc}")


@contextlib.asynccontextmanager
async def _open_ws_stream(socket: WebSocket, ctx: AppContext) -> AsyncIterator[Callable[[dict[str, Any]], None] | None]:
    """Validate origin/auth, accept the WebSocket, and run a background writer task."""
    if not is_allowed_origin(socket.headers.get("origin"), socket.headers.get("host"), dev=ctx.dev):
        await socket.close(code=1008)
        yield None
        return
    if ctx.auth_token is not None and not verify_token(extract_ws_token(socket), ctx.auth_token):
        await socket.close(code=WS_AUTH_CLOSE_CODE, reason="Unauthorized")
        yield None
        return
    await socket.accept()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def push(frame: dict[str, Any]) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(outbox.put_nowait, frame)

    async def write() -> None:
        while True:
            frame = await outbox.get()
            try:
                await socket.send_text(json.dumps(frame, ensure_ascii=False))
            except Exception:
                return

    writer = asyncio.create_task(write())
    try:
        yield push
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    manager = ctx.manager

    @router.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        async with _open_ws_stream(socket, ctx) as push:
            if push is None:
                return
            conn = _ConnState()
            manager.register_sink(push)
            try:
                while True:
                    raw = await socket.receive_text()
                    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                        _err(push, "Frame too large.")
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        _err(push, "Malformed JSON frame.")
                        continue
                    if not isinstance(message, dict):
                        _err(push, "Frame must be an object.")
                        continue
                    await _dispatch(manager, conn, socket.app, socket, message, push, ctx.sidebar)
            except WebSocketDisconnect:
                pass
            finally:
                manager.unregister_sink(push)
                if conn.session is not None:
                    conn.session.detach(push)

    @router.websocket("/ws/notifications")
    async def websocket_notifications(socket: WebSocket) -> None:
        """Read-only alert stream for Android's native background service."""
        async with _open_ws_stream(socket, ctx) as push:
            if push is None:
                return
            manager.add_notification_listener(push)
            try:
                from quartz.settings import load_settings

                loaded = await asyncio.to_thread(load_settings)
                push({
                    "type": "settings_state",
                    "notifications": loaded.notifications,
                    "notifyTurn": loaded.notify_turn,
                    "notifyApproval": loaded.notify_approval,
                    "notifyBackground": loaded.notify_background,
                    "notifyError": loaded.notify_error,
                    "richUi": loaded.rich_ui,
                })
                while True:
                    await socket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                manager.remove_notification_listener(push)

    return router
