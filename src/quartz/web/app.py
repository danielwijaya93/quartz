"""The `quartz web` HTTP + WebSocket app.

Three jobs, in this order of importance:

1. Serve the SPA, from `webapp/dist` in a checkout or the packaged copy in a wheel.
2. Move frames between a browser and one `WebSession`.
3. Fail visibly. A server that cannot build a runtime still serves the page and says
   why in the browser, because a terminal that exits has nowhere to put the message.

The wire format is `docs/web-protocol.md`. If a frame here disagrees with that
document, the document wins and this file is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from quartz.web.session import BuildRuntime, TurnBusyError, TurnRunner, WebSession
from quartz.core.turn import run_interactive_turn

__all__ = ["create_app", "resolve_static_dir"]

MAX_FRAME_BYTES = 1024 * 1024
"""Largest client frame accepted. The protocol says 1 MiB; see its Security section."""

_MISSING_BUILD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quartz web</title>
<style>
 body{{background:#0b0c0e;color:#e6e7ea;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-width:56rem;margin:12vh auto;padding:0 1.5rem}}
 h1{{font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;color:#9aa0a6}}
 code{{background:#171a1e;border:1px solid #262b31;padding:.15rem .35rem;border-radius:3px}}
 pre{{background:#171a1e;border:1px solid #262b31;padding:.9rem 1rem;overflow:auto}}
 a{{color:#7cc4ff}}
</style></head><body>
<h1>Quartz web — client not built</h1>
<p>The server is running, but there is no built client at <code>{where}</code>.</p>
<pre>cd webapp
npm ci
npm run build</pre>
<p>Then reload this page. For live editing, run <code>npm run dev</code> in
<code>webapp/</code> instead and use the Vite URL it prints.</p>
</body></html>
"""

_MISSING_DEV = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quartz web — dev</title>
<style>
 body{{background:#0b0c0e;color:#e6e7ea;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-width:56rem;margin:12vh auto;padding:0 1.5rem}}
 h1{{font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;color:#9aa0a6}}
 code{{background:#171a1e;border:1px solid #262b31;padding:.15rem .35rem;border-radius:3px}}
</style></head><body>
<h1>Quartz web — dev mode</h1>
<p>This server is API-only in dev mode. Run the client separately and point it at this
server:</p>
<pre>cd webapp &amp;&amp; npm run dev</pre>
<p>The Vite dev server proxies <code>/ws</code> here, so open the URL it prints.</p>
</body></html>
"""


def resolve_static_dir(explicit: Path | None = None) -> Path | None:
    """Find the built SPA, or ``None`` when nothing has been built yet.

    Three locations, in order, because this module has three lives: a test that
    passes a directory, a source checkout (`webapp/dist` next to `src/`), and an
    installed wheel where the build is copied inside the package. Returning ``None``
    rather than raising is deliberate: the caller shows a page, not a traceback.
    """
    # An explicit directory is authoritative, right or wrong: a caller that passes
    # one is stating where the client is, and silently falling back to some other
    # build would serve a different client than the one asked for (and makes tests
    # depend on whether `webapp/dist` happens to exist on the machine).
    if explicit is not None:
        return explicit if (explicit / "index.html").is_file() else None

    candidates: list[Path] = []
    env = os.environ.get("QUARTZ_WEB_STATIC")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parent / "_static")          # installed wheel
    candidates.append(here.parents[3] / "webapp" / "dist")  # source checkout
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    *,
    build: BuildRuntime,
    cwd: Path,
    static_dir: Path | None = None,
    dev: bool = False,
    runner: TurnRunner = run_interactive_turn,
) -> FastAPI:
    """Build the app around one `WebSession`.

    `build` and `runner` are injected rather than imported so tests can drive the
    whole server without a kernel, a provider or an API key -- which is also how the
    frame-parity test asserts that every `TurnObserver` hook reaches the wire.
    """
    session = WebSession(build, cwd=cwd, runner=runner)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Close the kernel on shutdown, on a thread: `close` blocks on a join."""
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.close)

    app = FastAPI(
        title="quartz web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.session = session
    app.state.cwd = cwd


    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "built": session.built,
                "turnRunning": session.turn_running,
                "cwd": str(cwd),
            }
        )

    @app.get("/api/sessions")
    async def sessions() -> JSONResponse:
        rows = await asyncio.to_thread(session.sessions)
        return JSONResponse({"sessions": rows})

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def push(frame: dict[str, Any]) -> None:
            """Hand a frame to the writer. Safe from a turn thread."""
            loop.call_soon_threadsafe(outbox.put_nowait, frame)

        async def write() -> None:
            while True:
                frame = await outbox.get()
                try:
                    await socket.send_text(json.dumps(frame, ensure_ascii=False))
                except Exception:  # noqa: BLE001 - the reader will notice and stop
                    return

        writer = asyncio.create_task(write())
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
                await _dispatch(session, socket, message, push)
        except WebSocketDisconnect:
            pass
        finally:
            # A closed tab must not leave a turn running against a kernel nobody is
            # reading. The turn is aborted and drained; the kernel stays alive so a
            # reconnect reconciles instead of losing the session.
            await asyncio.to_thread(session._abort_and_join)  # noqa: SLF001 - same package
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    static = resolve_static_dir(static_dir)
    if static is not None:
        app.mount("/", StaticFiles(directory=str(static), html=True), name="spa")
    else:
        body = _MISSING_DEV if dev else _MISSING_BUILD.format(where=static_dir or "webapp/dist")

        @app.get("/")
        async def index() -> HTMLResponse:
            return HTMLResponse(body, status_code=200 if dev else 503)

    return app


async def _dispatch(
    session: WebSession,
    socket: WebSocket,
    message: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
) -> None:
    """Route one client frame. Every failure becomes an `error` frame, never a 500."""
    kind = message.get("type")
    try:
        if kind == "hello":
            await _ensure(session, push)
            push(session.snapshot())

        elif kind == "prompt":
            text = str(message.get("text") or "").strip()
            if not text:
                push({"type": "error", "message": "Empty prompt.", "fatal": False})
                return
            await _ensure(session, push)
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
            session.start_turn(text, _attachments(message.get("images")), push)

        elif kind == "steer":
            text = str(message.get("text") or "").strip()
            if not text:
                return
            await _ensure(session, push)
            session.steer(text)

        elif kind == "cancel":
            await _ensure(session, push)
            session.cancel()

        elif kind == "command":
            await _ensure(session, push)
            name = str(message.get("name") or "")
            arg = str(message.get("arg") or "")
            ok, result = await asyncio.to_thread(session.run_command, name, arg)
            push(
                {
                    "type": "command_result",
                    "name": name.lstrip("/"),
                    "ok": ok,
                    "result": _command_payload(result),
                }
            )

        elif kind == "new_session":
            await _ensure(session, push)
            await asyncio.to_thread(session.new_session)
            push(session.snapshot())

        elif kind == "load_session":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(session.load_session, session_id)
            push(session.snapshot())

        else:
            push({"type": "error", "message": f"Unknown frame type: {kind}", "fatal": False})

    except WebSocketDisconnect:
        raise
    except _AlreadyReported:
        # `_ensure` already sent a fatal frame with the real reason. Letting this
        # fall through to the generic handler would show the user the name of this
        # private control-flow exception instead.
        return
    except TurnBusyError as exc:
        push({"type": "error", "message": str(exc), "fatal": False})
    except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the socket
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": False})


async def _ensure(session: WebSession, push: Callable[[dict[str, Any]], None]) -> None:
    """Build the runtime if needed; report a build failure as a fatal frame."""
    if session.built:
        return
    try:
        await asyncio.to_thread(session.ensure_runtime)
    except Exception as exc:  # noqa: BLE001 - surfaced to the browser, on purpose
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": True})
        raise _AlreadyReported from exc


class _AlreadyReported(Exception):
    """The failure has been sent to the client; the dispatcher should just stop."""


def _attachments(raw: Any) -> list[Any]:
    """Wire images as `ImageAttachment`s, ignoring anything malformed."""
    from quartz.core.images import ImageAttachment

    out: list[ImageAttachment] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        mime = item.get("mimeType") or item.get("mime_type")
        if isinstance(data, str) and data and isinstance(mime, str) and mime:
            out.append(ImageAttachment(mime_type=mime, data_b64=data))
    return out


def _command_payload(result: Any) -> dict[str, Any]:
    """Serialise a `CommandResult` to the shape the browser's card renders.

    `items` is the one place a transport may reshape: the core returns the values
    (a job's pid and command, a variable's name and type) and the terminal prints
    them as `key: value`. The browser's list wants `label`/`detail`, so the mapping
    happens here rather than in `core/`, where it would leak a presentation choice.
    """
    payload: dict[str, Any] = {"kind": result.kind}
    if result.title:
        payload["title"] = result.title
    if result.body:
        payload["body"] = result.body
    if result.message:
        payload["message"] = result.message
    if result.items:
        payload["items"] = [_list_item(item) for item in result.items]
    return payload


_PRIMARY_KEYS = ("label", "name", "pid", "id", "key")


def _list_item(item: dict[str, Any]) -> dict[str, str]:
    """One core list row as the browser's `{label, detail}` pair."""
    label = ""
    for key in _PRIMARY_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            label = str(value)
            break
    parts = [
        f"{key}: {value}"
        for key, value in item.items()
        if key not in _PRIMARY_KEYS or not label or str(item.get(key, "")) != label
        if isinstance(value, (str, int, float, bool)) and str(value) != ""
    ]
    return {"label": label, "detail": " · ".join(parts)}

