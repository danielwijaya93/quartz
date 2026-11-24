"""WebSocket `/ws` route and frame dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
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
    IdleSessionResponseFrame,
    LoadSessionFrame,
    NewSessionFrame,
    NotificationsReadFrame,
    PermissionResponseFrame,
    PromptFrame,
    SetSandboxFrame,
    SteerFrame,
    TrustProjectFrame,
    parse_client_frame,
)
from quartz.web.session import SessionManager, TurnBusyError, WebSession, resolve_cwd
from quartz.web.sidebar import SidebarStore


class _ConnState:
    """Per-WebSocket active session pointer."""

    def __init__(self) -> None:
        self.session: WebSession | None = None


def _switch_connection(
    conn: _ConnState,
    target: WebSession,
    push: Callable[[dict[str, Any]], None],
    app: FastAPI,
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
    """Ensure this connection has a built `WebSession`, reporting build errors fatally."""
    if conn.session is not None and conn.session.built:
        return conn.session
    try:
        target = await asyncio.to_thread(manager.ensure_default_session, cwd)
    except Exception as exc:
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": True})
        raise AlreadyReported from exc
    _switch_connection(conn, target, push, app)
    return target


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
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in CLIENT_FRAME_TYPES:
        push({"type": "error", "message": f"Unknown frame type: {kind}", "fatal": False})
        return
    try:
        frame = parse_client_frame(message)
    except ValidationError as exc:
        push({"type": "error", "message": f"Invalid frame: {exc}", "fatal": False})
        return

    try:
        if isinstance(frame, HelloFrame):
            manager.set_system_prompt_append(frame.system_prompt_append or "")
            requested_id = frame.session_id
            session: WebSession | None = None
            if isinstance(requested_id, str) and requested_id.strip():
                with contextlib.suppress(Exception):
                    session = await asyncio.to_thread(
                        manager.get_or_load_session, requested_id.strip()
                    )
            if session is None:
                session = await _ensure_conn_session(
                    manager,
                    conn,
                    push,
                    app,
                    resolve_cwd(frame.cwd, manager.cwd),
                )
            else:
                _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            from quartz.settings import load_settings

            loaded_settings = await asyncio.to_thread(load_settings)
            snapshot = session.snapshot()
            snapshot["settings"] = {
                "notifications": loaded_settings.notifications,
                "richUi": loaded_settings.rich_ui,
            }
            push(snapshot)
            # A tab that just (re)connected in the middle of a turn is told what the
            # turn is doing, and what it has written so far, in ordinary frames.
            _replay_live_turn(session, snapshot, push)
            for idle_frame in manager.pending_idle_prompts():
                push(idle_frame)
            active_sessions = manager.active_sessions_items()
            push({"type": "active_sessions_state", "items": active_sessions})

        elif isinstance(frame, PromptFrame):
            text = frame.text.strip()
            raw_images = [img.model_dump(by_alias=True) for img in (frame.images or [])]
            images = attachments_from_raw(raw_images)
            if not text and not images:
                push({"type": "error", "message": "Empty prompt.", "fatal": False})
                return
            session = await _ensure_conn_session(manager, conn, push, app)
            if session.turn_running:
                # The protocol rejects rather than queues: a queued prompt would
                # arrive with context the user has already moved past.
                push(
                    {
                        "type": "error",
                        "message": "A turn is already running. Press Esc to abort it.",
                        "fatal": False,
                    }
                )
                return
            session.bind_loop(asyncio.get_running_loop())
            session.start_turn(text, images)

        elif isinstance(frame, SteerFrame):
            text = frame.text.strip()
            if not text:
                return
            session = await _ensure_conn_session(manager, conn, push, app)
            session.steer(text)

        elif isinstance(frame, CancelFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            session.cancel()

        elif isinstance(frame, CommandFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            name = frame.name
            arg = frame.arg or ""
            before_id = session.runtime.active_session.id
            before_model = session.runtime.model
            before_effort = session.runtime.effort
            ok, result = await session.run_command_async(name, arg)
            push(
                {
                    "type": "command_result",
                    "name": name.lstrip("/"),
                    "ok": ok,
                    "result": command_payload(result),
                }
            )
            if (
                session.runtime.active_session.id != before_id
                or session.runtime.model != before_model
                or session.runtime.effort != before_effort
            ):
                push(session.snapshot())

        elif isinstance(frame, NewSessionFrame):
            # A folder picked in the browser. `None` means "the server's directory",
            # and an unusable value falls back to it rather than failing the frame.
            target = frame.cwd
            resolved_cwd = None if target is None else resolve_cwd(target, manager.cwd)
            session = await asyncio.to_thread(manager.create_session, resolved_cwd)
            _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            push(session.snapshot())
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif isinstance(frame, LoadSessionFrame):
            session_id = frame.session_id.strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            session = await asyncio.to_thread(manager.get_or_load_session, session_id)
            _switch_connection(conn, session, push, app)
            snapshot = session.snapshot()
            push(snapshot)
            _replay_live_turn(session, snapshot, push)
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif isinstance(frame, TrustProjectFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            await asyncio.to_thread(session.trust_current_project)
            push(session.snapshot())

        elif isinstance(frame, SetSandboxFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            await asyncio.to_thread(session.set_sandbox, frame.enabled)
            session.emit(session.snapshot())

        elif isinstance(frame, PermissionResponseFrame):
            session = await _ensure_conn_session(manager, conn, push, app)
            request_id = frame.request_id.strip()
            decision = frame.decision.strip()
            if not request_id:
                push({"type": "error", "message": "Missing requestId.", "fatal": False})
                return
            await asyncio.to_thread(session.resolve_permission, request_id, decision)

        elif isinstance(frame, IdleSessionResponseFrame):
            session_id = frame.session_id.strip()
            action = frame.action.strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.resolve_idle_prompt, session_id, action)

        elif isinstance(frame, ActiveSessionDismissFrame):
            session_id = frame.session_id.strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.dismiss_active_session, session_id)

        elif isinstance(frame, ActiveSessionsClearReadFrame):
            await asyncio.to_thread(manager.clear_read_active_sessions)

        elif isinstance(frame, NotificationsReadFrame):
            manager.broadcast_notifications_cleared()

    except WebSocketDisconnect:
        raise
    except AlreadyReported:
        # `ensure_session` already sent a fatal frame with the real reason. Letting this
        # fall through to the generic handler would show the user the name of this
        # private control-flow exception instead.
        return
    except TurnBusyError as exc:
        push({"type": "error", "message": str(exc), "fatal": False})
    except Exception as exc:
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": False})


def _replay_live_turn(
    session: WebSession,
    snapshot: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
) -> None:
    """Retell the turn in flight as frames, for a client that just arrived."""
    if not snapshot.get("turnRunning"):
        return
    turn_id = session.turn_id
    if turn_id is None:
        return
    push({"type": "turn_start", "turnId": turn_id})
    label = session.turn_status
    if label:
        push({"type": "status", "label": label})


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    manager = ctx.manager

    @router.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        if not is_allowed_origin(
            socket.headers.get("origin"),
            socket.headers.get("host"),
            dev=ctx.dev,
        ):
            await socket.close(code=1008)
            return
        if ctx.auth_token is not None:
            candidate = extract_ws_token(socket)
            if not verify_token(candidate, ctx.auth_token):
                await socket.close(code=WS_AUTH_CLOSE_CODE, reason="Unauthorized")
                return
        await socket.accept()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def push(frame: dict[str, Any]) -> None:
            """Hand a frame to the writer. Safe from a turn thread."""
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
        conn = _ConnState()
        manager.register_sink(push)
        try:
            while True:
                raw = await socket.receive_text()
                if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                    push({"type": "error", "message": "Frame too large.", "fatal": False})
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    push({"type": "error", "message": "Malformed JSON frame.", "fatal": False})
                    continue
                if not isinstance(message, dict):
                    push({"type": "error", "message": "Frame must be an object.", "fatal": False})
                    continue
                await _dispatch(manager, conn, socket.app, socket, message, push, ctx.sidebar)
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister_sink(push)
            if conn.session is not None:
                conn.session.detach(push)
                conn.session.begin_grace()
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    @router.websocket("/ws/notifications")
    async def websocket_notifications(socket: WebSocket) -> None:
        """Read-only alert stream for Android's native background service."""
        if not is_allowed_origin(
            socket.headers.get("origin"),
            socket.headers.get("host"),
            dev=ctx.dev,
        ):
            await socket.close(code=1008)
            return
        if ctx.auth_token is not None:
            candidate = extract_ws_token(socket)
            if not verify_token(candidate, ctx.auth_token):
                await socket.close(code=WS_AUTH_CLOSE_CODE, reason="Unauthorized")
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
        manager.add_notification_listener(push)
        try:
            from quartz.settings import load_settings

            loaded = await asyncio.to_thread(load_settings)
            push(
                {
                    "type": "settings_state",
                    "notifications": loaded.notifications,
                    "richUi": loaded.rich_ui,
                }
            )
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.remove_notification_listener(push)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    return router
