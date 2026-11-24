"""Sidebar session and workspace routes (`/api/sessions*`, `/api/workspaces*`)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from quartz.core.session import Session
from quartz.web.context import AppContext, bad_request, json_body, session_id_from_body
from quartz.web.files import resolve_directory
from quartz.web.sidebar import SidebarStore, sidebar_payload


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def sidebar_snapshot() -> dict[str, Any]:
        """Every known session across every cwd, plus the whole sidebar state."""
        rows, _cursor = Session.list_all(limit=None)
        return sidebar_payload(rows, ctx.sidebar().state)

    async def mutate(action: Callable[[SidebarStore], None]) -> JSONResponse:
        """Run one sidebar mutation off the loop, then return the fresh snapshot."""

        def run() -> dict[str, Any]:
            action(ctx.sidebar())
            return sidebar_snapshot()

        return JSONResponse(await asyncio.to_thread(run))

    @router.get("/api/sessions")
    async def sessions() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(sidebar_snapshot))

    @router.post("/api/sessions/hide")
    async def hide_session(request: Request) -> JSONResponse:
        """Archive: stop listing a session. No file on disk is touched but ours."""
        session_id = session_id_from_body(await json_body(request))
        if session_id is None:
            return bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.hide(session_id))

    @router.post("/api/sessions/restore")
    async def restore_session(request: Request) -> JSONResponse:
        session_id = session_id_from_body(await json_body(request))
        if session_id is None:
            return bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.restore(session_id))

    @router.post("/api/sessions/import")
    async def import_session(request: Request) -> JSONResponse:
        """List a session the user picked out of the archive."""
        session_id = session_id_from_body(await json_body(request))
        if session_id is None:
            return bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.import_session(session_id))

    @router.post("/api/sessions/pin")
    async def pin_session(request: Request) -> JSONResponse:
        """Pin a session to the top of the sidebar."""
        session_id = session_id_from_body(await json_body(request))
        if session_id is None:
            return bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.pin(session_id))

    @router.post("/api/sessions/unpin")
    async def unpin_session(request: Request) -> JSONResponse:
        """Unpin a session from the top of the sidebar."""
        session_id = session_id_from_body(await json_body(request))
        if session_id is None:
            return bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.unpin(session_id))

    @router.post("/api/sessions/rename")
    async def rename_session(request: Request) -> JSONResponse:
        body = await json_body(request)
        session_id = session_id_from_body(body)
        if session_id is None:
            return bad_request("sessionId is required")
        title = body.get("title")
        if not isinstance(title, str):
            return bad_request("title must be a string")
        return await mutate(lambda sidebar: sidebar.rename(session_id, title))

    @router.post("/api/workspaces")
    async def add_workspace(request: Request) -> JSONResponse:
        """Add a folder by hand, so an empty folder can be shown and used."""
        body = await json_body(request)
        resolved, error = await asyncio.to_thread(resolve_directory, body.get("cwd"), ctx.cwd)
        if resolved is None or error is not None:
            return bad_request(error or "cwd must be a directory")
        return await mutate(lambda sidebar: sidebar.add_workspace(str(resolved)))

    @router.post("/api/workspaces/remove")
    async def remove_workspace(request: Request) -> JSONResponse:
        """Remove a folder from the sidebar list without touching disk."""
        body = await json_body(request)
        raw_cwd = body.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd.strip():
            return bad_request("cwd is required")
        target = raw_cwd.strip()
        return await mutate(lambda sidebar: sidebar.remove_workspace(target))

    return router
