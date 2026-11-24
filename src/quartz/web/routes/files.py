"""Filesystem picker and attached-file routes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from quartz.web.attachments import is_safe_inline_mime
from quartz.web.context import AppContext, json_body
from quartz.web.files import create_directory, list_directories, list_workspace_files


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/fs/list")
    async def fs_list(path: str | None = None) -> JSONResponse:
        """Directories inside `path`. A bad path is `error` in the body, not a 500."""
        try:
            listing = await asyncio.to_thread(list_directories, path, ctx.cwd)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(listing.to_dict())

    @router.post("/api/fs/mkdir")
    async def fs_mkdir(request: Request) -> JSONResponse:
        """Create one folder inside `path`. The only mutation the picker has."""
        body = await json_body(request)
        try:
            listing = await asyncio.to_thread(
                create_directory, body.get("path"), body.get("name"), ctx.cwd
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(listing.to_dict())

    @router.get("/api/workspace/files")
    async def workspace_files(
        workspace_cwd: str | None = Query(default=None, alias="cwd"),
        limit: int = 2000,
    ) -> JSONResponse:
        """Relative file paths inside the active workspace for `@file` autocomplete."""
        current = ctx.manager.current_session
        active_cwd = current.runtime.cwd if current.built else ctx.cwd
        target = workspace_cwd or str(active_cwd)
        payload = await asyncio.to_thread(list_workspace_files, target, active_cwd, limit)
        return JSONResponse(payload)

    @router.get("/api/files/{file_id}")
    async def download_attached_file(
        file_id: str,
        inline: bool = Query(default=False),
    ) -> Response:
        """Serve a file that Quartz explicitly attached during a turn."""
        entry = await asyncio.to_thread(ctx.file_registry.get, file_id)
        if entry is None:
            return JSONResponse({"error": "File not found"}, status_code=404)
        media_type = entry.get("mimeType") or "application/octet-stream"
        disposition = "inline" if (inline and is_safe_inline_mime(media_type)) else "attachment"
        return FileResponse(
            path=entry["path"],
            media_type=media_type,
            filename=entry.get("name") or Path(entry["path"]).name,
            content_disposition_type=disposition,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return router
