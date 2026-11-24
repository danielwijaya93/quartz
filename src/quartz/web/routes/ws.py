"""WebSocket `/ws` route and frame dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from quartz.web.context import (
    MAX_FRAME_BYTES,
    AlreadyReported,
    AppContext,
    attachments_from_raw,
    command_payload,
    is_allowed_origin,
    remember_workspace,
    system_prompt_append,
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
    try:
        if kind == "hello":
            manager.set_system_prompt_append(system_prompt_append(message))
            requested_id = message.get("sessionId")
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
                    resolve_cwd(message.get("cwd"), manager.cwd),
                )
            else:
                _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            snapshot = session.snapshot()
            push(snapshot)
            # A tab that just (re)connected in the middle of a turn is told what the
            # turn is doing, and what it has written so far, in ordinary frames.
            _replay_live_turn(session, snapshot, push)
            for idle_frame in manager.pending_idle_prompts():
                push(idle_frame)
            active_sessions = manager.active_sessions_items()
            push({"type": "active_sessions_state", "items": active_sessions})

        elif kind == "prompt":
            text = str(message.get("text") or "").strip()
            images = attachments_from_raw(message.get("images"))
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
            session.start_turn(text, images)

        elif kind == "steer":
            text = str(message.get("text") or "").strip()
            if not text:
                return
            session = await _ensure_conn_session(manager, conn, push, app)
            session.steer(text)

        elif kind == "cancel":
            session = await _ensure_conn_session(manager, conn, push, app)
            session.cancel()

        elif kind == "command":
            session = await _ensure_conn_session(manager, conn, push, app)
            name = str(message.get("name") or "")
            arg = str(message.get("arg") or "")
            before_id = session.runtime.active_session.id
            before_model = session.runtime.model
            before_effort = session.runtime.effort
            ok, result = await asyncio.to_thread(session.run_command, name, arg)
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

        elif kind == "new_session":
            # A folder picked in the browser. `None` means "the server's directory",
            # and an unusable value falls back to it rather than failing the frame.
            target = message.get("cwd")
            resolved_cwd = None if target is None else resolve_cwd(target, manager.cwd)
            session = await asyncio.to_thread(manager.create_session, resolved_cwd)
            _switch_connection(conn, session, push, app)
            await asyncio.to_thread(remember_workspace, sidebar, session)
            push(session.snapshot())
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif kind == "load_session":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            session = await asyncio.to_thread(manager.get_or_load_session, session_id)
            _switch_connection(conn, session, push, app)
            snapshot = session.snapshot()
            push(snapshot)
            _replay_live_turn(session, snapshot, push)
            push({"type": "active_sessions_state", "items": manager.active_sessions_items()})

        elif kind == "trust_project":
            session = await _ensure_conn_session(manager, conn, push, app)
            await asyncio.to_thread(session.trust_current_project)
            push(session.snapshot())

        elif kind == "set_sandbox":
            session = await _ensure_conn_session(manager, conn, push, app)
            enabled = message.get("enabled")
            if not isinstance(enabled, bool):
                push(
                    {
                        "type": "error",
                        "message": "Missing boolean 'enabled' for set_sandbox.",
                        "fatal": False,
                    }
                )
                return
            await asyncio.to_thread(session.set_sandbox, enabled)
            session.emit(session.snapshot())

        elif kind == "permission_response":
            session = await _ensure_conn_session(manager, conn, push, app)
            request_id = str(message.get("requestId") or "").strip()
            decision = str(message.get("decision") or "reject").strip()
            if not request_id:
                push({"type": "error", "message": "Missing requestId.", "fatal": False})
                return
            await asyncio.to_thread(session.resolve_permission, request_id, decision)

        elif kind == "idle_session_response":
            session_id = str(message.get("sessionId") or "").strip()
            action = str(message.get("action") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.resolve_idle_prompt, session_id, action)

        elif kind == "active_session_dismiss":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.dismiss_active_session, session_id)

        elif kind == "active_sessions_clear_read":
            await asyncio.to_thread(manager.clear_read_active_sessions)

        else:
            push({"type": "error", "message": f"Unknown frame type: {kind}", "fatal": False})

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

    return router
