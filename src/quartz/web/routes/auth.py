"""Authentication routes (`/api/auth/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from quartz.web.auth import AUTH_COOKIE_NAME, extract_request_token, verify_token
from quartz.web.context import AppContext, json_body
from quartz.web.schemas import AuthLoginRequest

_COOKIE_MAX_AGE = 30 * 24 * 3600


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/status")
    async def auth_status(request: Request) -> JSONResponse:
        if ctx.auth_token is None:
            return JSONResponse({"authEnabled": False, "authenticated": True})
        candidate = extract_request_token(request, allow_query=False)
        return JSONResponse(
            {
                "authEnabled": True,
                "authenticated": verify_token(candidate, ctx.auth_token),
            }
        )

    @router.post("/api/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        raw = await json_body(request)
        try:
            payload = AuthLoginRequest.model_validate(raw)
        except ValidationError:
            return JSONResponse({"error": "Invalid token"}, status_code=401)

        if ctx.auth_token is not None and not verify_token(payload.token.strip(), ctx.auth_token):
            return JSONResponse({"error": "Invalid token"}, status_code=401)

        response = JSONResponse({"ok": True})
        if ctx.auth_token is not None:
            response.set_cookie(
                key=AUTH_COOKIE_NAME,
                value=ctx.auth_token,
                max_age=_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    @router.post("/api/auth/logout")
    async def auth_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
        return response

    return router
