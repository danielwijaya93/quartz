"""Agent Client Protocol (ACP) JSON-RPC 2.0 stdio server for quartz."""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from agent.images import content_to_text
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnUsage, agent_loop
from agent.prompt import build_system_prompt
from agent.session import Session

PROTOCOL_VERSION = 1


@dataclasses.dataclass
class _ActiveSession:
    session: Session
    kernel: KernelClient
    cwd: Path
    system_prompt: str
    messages: list[dict[str, Any]]
    usage: TurnUsage


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
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return ""
    sp = meta.get("systemPrompt")
    if not isinstance(sp, dict):
        return ""
    append = sp.get("append")
    return append.strip() if isinstance(append, str) else ""


class ACPServer:
    """JSON-RPC 2.0 ACP server speaking newline-delimited JSON over stdio."""

    def __init__(
        self,
        client_factory: Callable[[], tuple[Any, str, str, str]],
        *,
        in_stream: TextIO | None = None,
        out_stream: TextIO | None = None,
        max_context_tokens: int = 128_000,
        compact_reserve_tokens: int = 16_384,
        keep_recent_messages: int = 10,
    ) -> None:
        self._client_factory = client_factory
        self._in = in_stream or sys.stdin
        self._out = out_stream or sys.stdout
        self._write_lock = threading.Lock()
        self._sessions: dict[str, _ActiveSession] = {}
        self._sessions_lock = threading.Lock()
        self._max_context_tokens = max_context_tokens
        self._compact_reserve_tokens = compact_reserve_tokens
        self._keep_recent_messages = keep_recent_messages
        self._client: Any = None
        self._model = ""
        self._effort: str | None = None

    def _ensure_client(self) -> tuple[Any, str, str | None]:
        if self._client is None:
            self._client, _, self._model, self._effort = self._client_factory()
        return self._client, self._model, self._effort

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
                "promptCapabilities": {"image": True},
            },
        }

    def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_cwd = params.get("cwd")
        cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else Path.cwd().resolve()
        kernel = KernelClient(cwd=cwd)
        kernel.start()
        session = Session.create(cwd)
        system_prompt = build_system_prompt(cwd)
        append = _extract_system_prompt_append(params)
        if append:
            system_prompt = f"{system_prompt}\n\n{append}"
        state = _ActiveSession(
            session=session,
            kernel=kernel,
            cwd=cwd,
            system_prompt=system_prompt,
            messages=[],
            usage=TurnUsage(),
        )
        with self._sessions_lock:
            self._sessions[session.id] = state
        return {"sessionId": session.id}

    def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId", "")).strip()
        if not session_id:
            raise ValueError("Missing sessionId")
        session, messages = Session.load(session_id)
        raw_cwd = params.get("cwd")
        cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else Path.cwd().resolve()

        kernel = KernelClient(cwd=cwd)
        kernel.start()
        restored_notice = session.restore_kernel(kernel)
        if restored_notice:
            messages.append({"role": "user", "content": restored_notice})

        system_prompt = build_system_prompt(cwd)
        append = _extract_system_prompt_append(params)
        if append:
            system_prompt = f"{system_prompt}\n\n{append}"
        state = _ActiveSession(
            session=session,
            kernel=kernel,
            cwd=cwd,
            system_prompt=system_prompt,
            messages=messages,
            usage=TurnUsage.from_dict(session.last_usage),
        )
        with self._sessions_lock:
            self._sessions[session.id] = state

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if not content:
                continue
            if role == "user":
                self._send_update(
                    session.id,
                    {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": content_to_text(content)},
                    },
                )
            elif role == "assistant":
                self._send_update(
                    session.id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": str(content)},
                    },
                )

        return {"sessionId": session.id}

    def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId", "")).strip()
        with self._sessions_lock:
            state = self._sessions.get(session_id)
        if state is None:
            raise KeyError(f"Unknown sessionId: {session_id}")

        prompt_content = _extract_prompt_content(params.get("prompt"))
        state.messages.append({"role": "user", "content": prompt_content})

        client, model, effort = self._ensure_client()
        active_tc_ids: list[str] = []

        def _on_text_delta(chunk: str) -> None:
            self._send_update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk},
                },
            )

        def _on_tool_call(code: str) -> None:
            tc_id = f"tc_{uuid.uuid4().hex[:8]}"
            active_tc_ids.append(tc_id)
            self._send_update(
                session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tc_id,
                    "title": "ipython",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"code": code},
                },
            )

        def _on_tool_result(res: CellResult) -> None:
            tc_id = active_tc_ids.pop() if active_tc_ids else f"tc_{uuid.uuid4().hex[:8]}"
            self._send_update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tc_id,
                    "status": "completed" if res.ok else "failed",
                    "rawOutput": res.format_for_model(),
                },
            )

        try:
            _, updated_msgs, _ = agent_loop(
                client,
                model,
                state.system_prompt,
                state.messages,
                state.kernel,
                max_context_tokens=self._max_context_tokens,
                compact_reserve_tokens=self._compact_reserve_tokens,
                keep_recent_messages=self._keep_recent_messages,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                on_text_delta=_on_text_delta,
                usage_stats=state.usage,
                initial_prompt_tokens=state.usage.prompt_tokens,
                effort=effort,
            )
            state.messages = updated_msgs
            state.session.save_messages(state.messages)
            if state.usage.prompt_tokens > 0 or state.usage.total_prompt_tokens > 0:
                state.session.save_usage(state.usage.to_dict())
            state.session.save_kernel(state.kernel)
            return {"stopReason": "end_turn"}
        except KeyboardInterrupt:
            return {"stopReason": "cancelled"}

    def _handle_session_cancel(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        with self._sessions_lock:
            state = self._sessions.get(session_id)
        if state is not None:
            state.kernel.interrupt()

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

                if req.get("method") == "session/prompt":
                    t = threading.Thread(
                        target=self._dispatch_request, args=(req,), daemon=True
                    )
                    t.start()
                    workers.append(t)
                else:
                    self._dispatch_request(req)
        finally:
            for t in workers:
                t.join(timeout=2.0)
            with self._sessions_lock:
                for state in self._sessions.values():
                    try:
                        if state.messages:
                            state.session.save_kernel(state.kernel)
                    except Exception:
                        pass
                    state.kernel.close()
                self._sessions.clear()
