"""Session replay rendering for resumed conversations."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Group
from rich.text import Text

from quartz.core.images import content_to_text
from quartz.kernel.client import CellResult
from quartz.tui.render import (
    CellGroupBuffer,
    _active_printer,
    _render_bash_notice,
    _render_tool_call,
    _render_tool_result,
    render_user_prompt,
)


def _replay_session(
    messages: list[dict[str, Any]],
    cell_history: list[tuple[str, CellResult, float]],
    *,
    verbose: bool = False,
    cell_meta: dict[str, dict[str, Any]] | None = None,
    steering_indices: set[int] | None = None,
    show_thinking: bool = False,
    show_details: bool = False,
) -> None:
    """Replay a resumed session's full conversation and tool cells into the terminal."""
    p = _active_printer()
    p.reset()
    cell_group = CellGroupBuffer()
    tool_outputs: dict[str, str] = {
        str(m["tool_call_id"]): str(m.get("content", ""))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            cell_group.flush()
            text = content_to_text(content)
            if (
                (text.startswith("[") and text.endswith("]")) or text.startswith(("[bash-done", "[bash-status", "[progress]", "[compaction-summary]", "[python-state"))
            ):
                _render_bash_notice(text)
            elif steering_indices and idx in steering_indices:
                preview = text[:60] + ("..." if len(text) > 60 else "")
                p.block(Text(f"[steering] {preview}", style="cyan"))
            else:
                p.block(render_user_prompt(text))
        elif role == "assistant":
            reasoning = msg.get("reasoning") or ""
            if show_thinking and isinstance(reasoning, str) and reasoning.strip():
                cell_group.flush()
                clamped = reasoning[:2000]
                suffix = "… (truncated)" if len(reasoning) > 2000 else ""
                p.block(
                    Group(
                        Text("Thought:", style="dim"),
                        Text(clamped + suffix, style="dim"),
                    )
                )
            if content:
                cell_group.flush()
                p.markdown(str(content))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                if fn.get("name") != "ipython":
                    continue
                try:
                    code = json.loads(fn.get("arguments") or "{}").get("code", "")
                except json.JSONDecodeError:
                    code = fn.get("arguments") or ""
                tc_id = str(tc.get("id", ""))
                raw_out = tool_outputs.get(tc_id, "")
                meta = (cell_meta or {}).get(tc_id)
                if meta is not None:
                    is_ok = bool(meta.get("ok", True))
                    duration = float(meta.get("duration_s", 0.0))
                else:
                    is_err = (
                        "[stderr]" in raw_out or raw_out.startswith(("[error]", "[aborted", "[interrupted"))
                    )
                    is_ok = not is_err
                    duration = 0.0
                cell_res = CellResult(
                    ok=is_ok,
                    stdout="" if not is_ok else raw_out,
                    stderr=raw_out if not is_ok else "",
                    result=None,
                )
                cell_history.append((code, cell_res, duration))
                if verbose or show_details:
                    cell_group.flush()
                    _render_tool_call(code)
                    _render_tool_result(cell_res)
                else:
                    cell_group.add(len(cell_history), code, cell_res, duration)
    cell_group.flush()


