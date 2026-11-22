"""Agent Client Protocol (ACP) JSON-RPC 2.0 stdio server for quartz."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from quartz.bootstrap import BootstrapOptions, build_runtime
from quartz.core.compact import DEFAULT_KEEP_RECENT_TOKENS
from quartz.core.images import content_to_text
from quartz.core.loop import TurnAborted, TurnUsage, agent_loop
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus, TurnView, run_interactive_turn
from quartz.kernel.client import CellResult, KernelClient

PROTOCOL_VERSION = 1


@dataclasses.dataclass
class _ActiveSession:
    runtime: AgentRuntime
    system_prompt_base: str
    prompt_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    abort_event: threading.Event = dataclasses.field(default_factory=threading.Event)

    @property
    def session(self) -> Session:
        return self.runtime.active_session

    @property
    def kernel(self) -> KernelClient:
        return self.runtime.kernel

    @property
    def cwd(self) -> Path:
        return self.runtime.cwd

    @property
    def system_prompt(self) -> str:
        return self.runtime.system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self.runtime.system_prompt = value

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.runtime.messages

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        self.runtime.messages = value

    @property
    def usage(self) -> TurnUsage:
        return self.runtime.usage_stats

    @usage.setter
    def usage(self, value: TurnUsage) -> None:
        self.runtime.usage_stats = value

    def apply_system_prompt_append(self, append: str) -> None:
        clean = append.strip()
        if clean:
            self.runtime.system_prompt = f"{self.system_prompt_base}\n\n{clean}"
        else:
            self.runtime.system_prompt = self.system_prompt_base


def _extract_prompt_content(prompt: Any) -> str | list[dict[str, Any]]:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts: list[dict[str, Any]] = []
        has_image = False
        for block in prompt:
            if isinstance(block, str):
                if block:
                    parts.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                if text:
                    parts.append({"type": "text", "text": text})
            elif btype == "image":
                data = str(block.get("data", "")).strip()
                mime = str(block.get("mimeType") or block.get("mime_type") or "image/png").strip()
                if data:
                    has_image = True
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        if not has_image:
            return "\n".join(p["text"] for p in parts if p.get("text"))
        return parts
    return str(prompt or "")


def _extract_system_prompt_append(params: dict[str, Any]) -> str:
    """Return the client-supplied system-prompt append from ``params["_meta"]``.

    Quartz's vendor extension follows the ACP ``_meta`` namespacing convention:
    ``{"_meta": {"quartz": {"systemPrompt": {"append": "..."}}}}``.
    """
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return ""
    quartz = meta.get("quartz")
    if not isinstance(quartz, dict):
        return ""
    sp = quartz.get("systemPrompt")
    if not isinstance(sp, dict):
        return ""
    append = sp.get("append")
    return append.strip() if isinstance(append, str) else ""


def _image_content_blocks(images: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Convert kernel/MCP image dicts into ACP `tool_call_update` content blocks."""
    return [
        {
            "type": "content",
            "content": {"type": "image", "data": img["data"], "mimeType": img["mime_type"]},
        }
        for img in images or []
        if img.get("data") and img.get("mime_type")
    ]


class _AcpObserver(TurnView):
    """Streams turn events to the ACP client as ``session/update`` notifications.

    Only the events ACP renders today are implemented. ``on_status``,
    ``on_thinking_delta``, ``on_compaction``, ``on_bash_notice`` and ``on_steering``
    stay unimplemented on purpose: the ACP event mapping for thinking and
    compaction is a tracked follow-up.
    """

    def __init__(
        self,
        server: ACPServer,
        session_id: str,
        runtime: AgentRuntime | None = None,
    ) -> None:
        super().__init__()
        self._server = server
        self._session_id = session_id
        self._runtime = runtime
        self._tc_map: dict[str, str] = {}
        self._pending_tools: dict[str, tuple[str, float]] = {}

    def on_text_delta(self, text: str) -> None:
        self.streamed_any[0] = True
        self._server._send_update(
            self._session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    def show_final(self, answer: str) -> None:
        if answer:
            self._server._send_update(
                self._session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": answer},
                },
            )

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        import time as _time

        acp_tc_id = tool_call_id or f"tc_{uuid.uuid4().hex[:8]}"
        self._tc_map[tool_call_id] = acp_tc_id
        self._pending_tools[tool_call_id] = (code, _time.monotonic())
        self._server._send_update(
            self._session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": acp_tc_id,
                "title": "ipython",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"code": code},
            },
        )

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        import time as _time

        code, started = self._pending_tools.pop(tool_call_id, ("", _time.monotonic()))
        duration_s = _time.monotonic() - started
        if self._runtime is not None:
            self._runtime.cell_history.append((code, cell, duration_s))

        acp_tc_id = self._tc_map.pop(tool_call_id, tool_call_id or f"tc_{uuid.uuid4().hex[:8]}")
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": acp_tc_id,
            "status": "completed" if cell.ok else "failed",
            "rawOutput": cell.format_for_model(),
        }
        image_blocks = _image_content_blocks(cell.images)
        if image_blocks:
            update["content"] = image_blocks
        self._server._send_update(self._session_id, update)


class ACPServer:
    """JSON-RPC 2.0 ACP server speaking newline-delimited JSON over stdio."""

    def __init__(
        self,
        client_factory: Callable[[], tuple[Any, ...]] | None = None,
        *,
        build_runtime_fn: Callable[[BootstrapOptions], tuple[AgentRuntime, bool]] | None = None,
        turn_runner: Callable[..., TurnOutcome] = run_interactive_turn,
        in_stream: TextIO | None = None,
        out_stream: TextIO | None = None,
        max_context_tokens: int | None = None,
        compact_reserve_tokens: int = 16_384,
        keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
        progress_every: int = 0,
        background_check_every: int = 0,
    ) -> None:
        self._client_factory = client_factory
        self._build_runtime_fn = build_runtime_fn or build_runtime
        self._runner = turn_runner
        self._in = in_stream or sys.stdin
        self._out = out_stream or sys.stdout
        self._write_lock = threading.Lock()
        self._sessions: dict[str, _ActiveSession] = {}
        self._sessions_lock = threading.Lock()
        self._max_context_tokens = max_context_tokens
        self._compact_reserve_tokens = compact_reserve_tokens
        self._keep_recent_tokens = keep_recent_tokens
        self._progress_every = progress_every
        self._background_check_every = background_check_every
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._provider_name = ""
        self._model = ""
        self._effort: str | None = None

    def _ensure_client(self) -> tuple[Any, str, str | None]:
        with self._client_lock:
            if self._client is None and self._client_factory is not None:
                result = self._client_factory()
                if len(result) == 5:
                    self._client, self._provider_name, self._model, self._effort, resolved_ctx = result
                    if resolved_ctx is not None:
                        self._max_context_tokens = int(resolved_ctx)
                else:
                    self._client, self._provider_name, self._model, self._effort = result[:4]
            return self._client, self._model, self._effort

    def _ensure_client_tuple(self) -> tuple[Any, str, str, str | None, int | None]:
        self._ensure_client()
        return (
            self._client,
            self._provider_name,
            self._model,
            self._effort,
            self._max_context_tokens,
        )

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _send_result(self, req_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _send_error(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def _send_update(self, session_id: str, update: dict[str, Any]) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentInfo": {"name": "quartz", "version": "0.1.0"},
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {
                    "list": {},
                },
                "promptCapabilities": {"image": True},
                "_meta": {
                    "quartz": {
                        "systemPrompt": {"append": True},
                    },
                },
            },
        }

    @staticmethod
    def _abort_session(state: _ActiveSession) -> None:
        state.abort_event.set()
        state.kernel.interrupt()

    def _retire_session(self, state: _ActiveSession, wait: float = 5.0) -> None:
        """Abort any in-flight prompt, wait for it to release the session, then close its kernel."""
        self._abort_session(state)
        acquired = state.prompt_lock.acquire(timeout=wait)
        try:
            state.runtime.wait_snapshot()
            if state.runtime.messages and not state.runtime.headless and state.runtime.kernel.dirty:
                with contextlib.suppress(Exception):
                    state.runtime.active_session.save_kernel(state.runtime.kernel)
            state.kernel.close()
            with contextlib.suppress(Exception):
                state.runtime.client.close()
        finally:
            if acquired:
                state.prompt_lock.release()

    def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_cwd = params.get("cwd")
        cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else Path.cwd().resolve()
        factory = self._ensure_client_tuple if self._client_factory is not None else None
        options = BootstrapOptions(
            cwd=cwd,
            origin="acp",
            transport="acp",
            headless=False,
            progress_every=self._progress_every,
            background_check_every=self._background_check_every,
            compact_reserve_tokens=self._compact_reserve_tokens,
            keep_recent_tokens=self._keep_recent_tokens,
            max_context_tokens=self._max_context_tokens,
            client_factory=factory,
            kernel_factory=KernelClient,
            lazy_provider=True,
        )
        runtime, _ = self._build_runtime_fn(options)
        state = _ActiveSession(runtime=runtime, system_prompt_base=runtime.system_prompt)
        append = _extract_system_prompt_append(params)
        if append:
            state.apply_system_prompt_append(append)

        with self._sessions_lock:
            old_state = self._sessions.pop(runtime.active_session.id, None)
            self._sessions[runtime.active_session.id] = state
        if old_state is not None:
            self._retire_session(old_state)
        return {"sessionId": runtime.active_session.id}

    def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId", "")).strip()
        if not session_id:
            raise ValueError("Missing sessionId")
        raw_cwd = params.get("cwd")
        cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else Path.cwd().resolve()

        factory = self._ensure_client_tuple if self._client_factory is not None else None
        options = BootstrapOptions(
            cwd=cwd,
            resume_id=session_id,
            origin="acp",
            transport="acp",
            headless=False,
            progress_every=self._progress_every,
            background_check_every=self._background_check_every,
            compact_reserve_tokens=self._compact_reserve_tokens,
            keep_recent_tokens=self._keep_recent_tokens,
            max_context_tokens=self._max_context_tokens,
            client_factory=factory,
            kernel_factory=KernelClient,
            lazy_provider=True,
        )
        runtime, _ = self._build_runtime_fn(options)
        state = _ActiveSession(runtime=runtime, system_prompt_base=runtime.system_prompt)
        append = _extract_system_prompt_append(params)
        if append:
            state.apply_system_prompt_append(append)

        with self._sessions_lock:
            old_state = self._sessions.pop(runtime.active_session.id, None)
            self._sessions[runtime.active_session.id] = state
        if old_state is not None:
            self._retire_session(old_state)

        for msg in runtime.messages:
            role = msg.get("role")
            content = msg.get("content")
            if not content:
                continue
            if role == "user":
                self._send_update(
                    runtime.active_session.id,
                    {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": content_to_text(content)},
                    },
                )
            elif role == "assistant":
                self._send_update(
                    runtime.active_session.id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": str(content)},
                    },
                )

        return {"sessionId": runtime.active_session.id}

    def _handle_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_cwd = params.get("cwd")
        cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else None
        cursor = params.get("cursor")
        cursor = cursor if isinstance(cursor, str) and cursor else None
        sessions, next_cursor = Session.list_all(cwd, cursor=cursor)
        result: dict[str, Any] = {"sessions": sessions}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId", "")).strip()
        with self._sessions_lock:
            state = self._sessions.get(session_id)
        if state is None:
            raise KeyError(f"Unknown sessionId: {session_id}")

        with state.prompt_lock:
            state.abort_event.clear()
            prompt_content = _extract_prompt_content(params.get("prompt"))
            state.runtime.messages.append({"role": "user", "content": prompt_content})

            observer = _AcpObserver(self, session_id, runtime=state.runtime)
            outcome = self._runner(
                state.runtime,
                observer,
                abort_event=state.abort_event,
                loop_fn=agent_loop,
            )

            if outcome.status == TurnStatus.ERROR and outcome.error is not None:
                raise outcome.error
            if outcome.status in (TurnStatus.ABORTED, TurnStatus.INTERRUPTED):
                return {"stopReason": "cancelled"}
            return {"stopReason": "end_turn"}

    def _handle_session_cancel(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        with self._sessions_lock:
            state = self._sessions.get(session_id)
        if state is not None:
            self._abort_session(state)

    def _dispatch_request(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        try:
            if method == "initialize":
                res = self._handle_initialize(params)
            elif method == "session/new":
                res = self._handle_session_new(params)
            elif method == "session/load":
                res = self._handle_session_load(params)
            elif method == "session/list":
                res = self._handle_session_list(params)
            elif method == "session/prompt":
                res = self._handle_session_prompt(params)
            elif method == "session/cancel":
                self._handle_session_cancel(params)
                if req_id is None:
                    return
                res = {}
            else:
                if req_id is not None:
                    self._send_error(req_id, -32601, f"Method not found: {method}")
                return

            if req_id is not None:
                self._send_result(req_id, res)
        except (KeyError, ValueError) as exc:
            if req_id is not None:
                message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
                self._send_error(req_id, -32602, message)
        except Exception as exc:
            if req_id is not None:
                self._send_error(req_id, -32000, str(exc))

    def serve(self) -> None:
        """Read JSON-RPC 2.0 requests from stdin until EOF."""
        workers: list[threading.Thread] = []
        try:
            for raw_line in self._in:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._send_error(None, -32700, f"Parse error: {exc}")
                    continue
                if not isinstance(req, dict):
                    self._send_error(None, -32600, "Invalid Request")
                    continue

                if req.get("method") == "session/prompt":
                    workers = [w for w in workers if w.is_alive()]
                    t = threading.Thread(
                        target=self._dispatch_request, args=(req,), daemon=True
                    )
                    t.start()
                    workers.append(t)
                else:
                    self._dispatch_request(req)
        finally:
            with self._sessions_lock:
                active = list(self._sessions.values())
            for state in active:
                self._abort_session(state)
            for t in workers:
                t.join(timeout=2.0)
            with self._client_lock:
                client = self._client
                self._client = None
            if client is not None and hasattr(client, "close"):
                with contextlib.suppress(Exception):
                    client.close()
            with self._sessions_lock:
                for state in self._sessions.values():
                    try:
                        state.runtime.wait_snapshot()
                        if state.messages and not state.runtime.headless and state.kernel.dirty:
                            state.session.save_kernel(state.kernel)
                    except Exception:
                        pass
                    state.kernel.close()
                    with contextlib.suppress(Exception):
                        state.runtime.client.close()
                self._sessions.clear()
