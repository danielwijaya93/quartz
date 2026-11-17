"""CLI entrypoint for quartz (interactive REPL, `-p` headless mode, and `mcp` subcommands)."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text

from agent.compact import compact_messages
from agent.completer import AgentCompleter
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnUsage, agent_loop, format_bash_done_events
from agent.mcp_client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from agent.prompt import build_system_prompt
from agent.session import Session

console = Console()
_WAKEUP_SENTINEL = "__MINIMAL_AGENT_WAKEUP__"


class MarkdownBlockStreamer:
    """Buffers streamed text and emits complete Markdown blocks without cursor-up redraws."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk.replace("\r\n", "\n")
        if "\n" not in self._buf:
            return

        # Keep any incomplete trailing line in self._buf
        last_nl = self._buf.rfind("\n")
        complete = self._buf[: last_nl + 1]
        tail = self._buf[last_nl + 1 :]

        in_fence = False
        block_lines: list[str] = []
        last_flushed_idx = 0
        lines = complete.splitlines(keepends=True)

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            block_lines.append(line)
            if not in_fence and stripped == "":
                block = "".join(block_lines).strip()
                if block:
                    self._emit(block)
                block_lines.clear()
                last_flushed_idx = idx + 1

        self._buf = "".join(lines[last_flushed_idx:]) + tail

    def flush(self) -> None:
        block = self._buf.strip()
        self._buf = ""
        if block:
            self._emit(block)


def _render_tool_call(code: str) -> None:
    syntax = Syntax(code.rstrip(), "python", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title="[bold cyan]ipython[/bold cyan]", border_style="cyan"))


def _render_tool_result(res: CellResult) -> None:
    text = res.format_for_model()
    style = "green" if res.ok else "red"
    title = "[bold green]out[/bold green]" if res.ok else "[bold red]error[/bold red]"
    console.print(Panel(text, title=title, border_style=style))


def format_compact_cell_summary(
    index: int, code: str, res: CellResult, duration: float
) -> str:
    """Format a 1-line collapsible summary chip for an executed ipython cell."""
    code_lines = [ln.strip() for ln in code.strip().splitlines() if ln.strip()]
    code_preview = (
        (code_lines[0][:50] + "...")
        if code_lines and len(code_lines[0]) > 50
        else (code_lines[0] if code_lines else "")
    )
    out_text = res.format_for_model().strip()
    out_lines = out_text.splitlines() if out_text and out_text != "(ok, no output)" else []

    if res.ok:
        out_summary = (
            f"{len(out_lines)} lines out"
            if len(out_lines) > 1
            else (out_lines[0][:50] if out_lines else "no output")
        )
        return f"✔ [#{index}] ipython ({len(code_lines)}L: {code_preview}) → {out_summary} ({duration:.2f}s)"
    err_lines = [ln.strip() for ln in (res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:70] if err_lines else "error"
    return f"✖ [#{index}] ipython ({len(code_lines)}L: {code_preview}) → {err_preview} ({duration:.2f}s)"


def _render_compaction(summarized: int, kept: int) -> None:
    console.print(
        f"[yellow][compact] Summarized {summarized} older messages; kept {kept} recent messages and Python kernel state.[/yellow]"
    )


def _render_bash_notice(notice: str) -> None:
    console.print(Panel(Text(notice), title="[bold magenta]harness notice[/bold magenta]", border_style="magenta"))


def _replay_session(
    messages: list[dict[str, Any]],
    cell_history: list[tuple[str, CellResult, float]],
    *,
    verbose: bool = False,
) -> None:
    """Replay a resumed session's full conversation and tool cells into the terminal."""
    import json

    tool_outputs: dict[str, str] = {
        str(m["tool_call_id"]): str(m.get("content", ""))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            text = str(content)
            if text.startswith("[") and text.endswith("]") or text.startswith("[bash-done") or text.startswith("[compaction-summary]") or text.startswith("[python-state"):
                _render_bash_notice(text)
            else:
                console.print(f"\n[bold]> {text}[/bold]")
        elif role == "assistant":
            if content:
                console.print(Markdown(str(content)))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                if fn.get("name") != "ipython":
                    continue
                try:
                    code = json.loads(fn.get("arguments") or "{}").get("code", "")
                except json.JSONDecodeError:
                    code = fn.get("arguments") or ""
                raw_out = tool_outputs.get(tc.get("id", ""), "")
                is_err = "[stderr]" in raw_out and "Traceback" in raw_out
                cell_res = CellResult(
                    ok=not is_err,
                    stdout="" if is_err else raw_out,
                    stderr=raw_out if is_err else "",
                    result=None,
                )
                cell_history.append((code, cell_res, 0.0))
                if verbose:
                    _render_tool_call(code)
                    _render_tool_result(cell_res)
                else:
                    summary = format_compact_cell_summary(len(cell_history), code, cell_res, 0.0)
                    style = "dim green" if cell_res.ok else "red"
                    console.print(f"[{style}]{summary}[/{style}]")


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _format_turn_footer(usage: TurnUsage, max_context_tokens: int) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    return f"(ctx: {_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%) | cache: {cache_pct:.0f}%)"


def _format_toolbar(
    model: str,
    session_id: str,
    usage: TurnUsage,
    max_context_tokens: int,
    bg_count: int,
) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    parts = [
        f" {model}",
        f"session:{session_id}",
        f"ctx:{_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%)",
        f"cache:{cache_pct:.0f}%",
    ]
    if bg_count > 0:
        parts.append(f"bg:{bg_count}")
    return " | ".join(parts) + " "


def _build_client() -> tuple[OpenAI, str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print(
            "[red]Error: OPENAI_API_KEY is not set. Add it to ~/.quartz/.env or .env.[/red]"
        )
        sys.exit(1)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("MODEL", "gpt-4.1")
    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


def _parse_kv_pairs(items: list[str], flag_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            console.print(f"[red]Error: {flag_name} expects KEY=VALUE, got {item!r}[/red]")
            sys.exit(2)
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def _handle_mcp_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz mcp",
        description="Manage MCP servers in ~/.quartz/mcp.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="List configured MCP servers")

    add_p = sub.add_parser("add", help="Add or update an MCP server")
    add_p.add_argument("name", help="Server name")
    add_p.add_argument("--url", help="Streamable HTTP MCP server URL")
    add_p.add_argument("--header", action="append", default=[], help="HTTP header (KEY=VALUE)")
    add_p.add_argument("--env", action="append", default=[], help="Environment variable (KEY=VALUE)")
    add_p.add_argument("--cwd", help="Working directory for stdio server")
    add_p.add_argument("--disabled", action="store_true", help="Add server as disabled")
    add_p.add_argument("cmd", nargs="*", help="Stdio command and args (after --)")

    rm_p = sub.add_parser("remove", help="Remove an MCP server")
    rm_p.add_argument("name", help="Server name to remove")

    en_p = sub.add_parser("enable", help="Enable one or more MCP servers")
    en_p.add_argument("names", nargs="*", help="Server name(s) to enable")
    en_p.add_argument("--all", action="store_true", help="Enable all configured servers")

    dis_p = sub.add_parser("disable", help="Disable one or more MCP servers")
    dis_p.add_argument("names", nargs="*", help="Server name(s) to disable")
    dis_p.add_argument("--all", action="store_true", help="Disable all configured servers")

    args = parser.parse_args(argv)
    cfg_path = default_mcp_config_path()

    if args.action == "list":
        servers = load_raw_mcp_config(cfg_path)
        if not servers:
            console.print(f"[dim]No MCP servers configured in {cfg_path}[/dim]")
            return
        for name, spec in sorted(servers.items()):
            enabled = spec.get("enabled", True) is not False
            status = "[green]enabled[/green]" if enabled else "[yellow]disabled[/yellow]"
            if "url" in spec:
                target = f"http: {spec['url']}"
            else:
                cmd = spec.get("command", "")
                cmd_args = " ".join(str(a) for a in spec.get("args", []))
                target = f"stdio: {cmd} {cmd_args}".strip()
            extras: list[str] = []
            if spec.get("headers") or spec.get("x-api-key"):
                extras.append("headers")
            if spec.get("env"):
                extras.append("env")
            extra_str = f" [dim]({', '.join(extras)})[/dim]" if extras else ""
            console.print(f"  [cyan]{name}[/cyan] [{status}] — {target}{extra_str}")
        return

    if args.action == "remove":
        if remove_mcp_server(args.name, cfg_path):
            console.print(f"[green]Removed MCP server '{args.name}' from {cfg_path}[/green]")
        else:
            console.print(f"[red]MCP server '{args.name}' not found in {cfg_path}[/red]")
            sys.exit(1)
        return

    if args.action in ("enable", "disable"):
        target_enabled = args.action == "enable"
        if args.all:
            targets = sorted(load_raw_mcp_config(cfg_path).keys())
        else:
            targets = list(args.names)
        if not targets:
            console.print(f"[red]Error: specify server name(s) or --all[/red]")
            sys.exit(2)
        missing: list[str] = []
        for name in targets:
            if not set_mcp_server_enabled(name, target_enabled, cfg_path):
                missing.append(name)
        verb = "Enabled" if target_enabled else "Disabled"
        updated = [n for n in targets if n not in missing]
        if updated:
            console.print(f"[green]{verb} MCP server(s): {', '.join(updated)}[/green]")
        if missing:
            console.print(f"[red]Not found in {cfg_path}: {', '.join(missing)}[/red]")
            sys.exit(1)
        return

    if args.action == "add":
        spec: dict[str, Any] = {}
        if args.url:
            spec["url"] = args.url
            if args.header:
                spec["headers"] = _parse_kv_pairs(args.header, "--header")
        else:
            cmd_parts = list(args.cmd[1:]) if args.cmd and args.cmd[0] == "--" else list(args.cmd)
            if not cmd_parts:
                console.print("[red]Error: provide --url or a stdio command (e.g. -- npx ...)[/red]")
                sys.exit(2)
            spec["command"] = cmd_parts[0]
            if len(cmd_parts) > 1:
                spec["args"] = cmd_parts[1:]
            if args.env:
                spec["env"] = _parse_kv_pairs(args.env, "--env")
            if args.cwd:
                spec["cwd"] = args.cwd

        if args.disabled:
            spec["enabled"] = False

        saved_path = add_mcp_server(args.name, spec, cfg_path)
        console.print(f"[green]Saved MCP server '{args.name}' to {saved_path}[/green]")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        _handle_mcp_cli(sys.argv[2:])
        return

    quartz_env = Path.home() / ".quartz" / ".env"
    if quartz_env.is_file():
        load_dotenv(quartz_env, override=False)
    load_dotenv(override=False)

    parser = argparse.ArgumentParser(description="quartz — minimal coding agent with a persistent Python REPL.")
    parser.add_argument("-p", "--prompt", help="Run a single prompt in headless mode and exit.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for the agent.")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="Resume the most recent session.")
    parser.add_argument("-r", "--resume", metavar="ID", help="Resume a specific session by ID.")
    parser.add_argument("--fork", action="store_true", help="Fork the resumed session into a new session ID.")
    parser.add_argument(
        "--background-wait",
        type=float,
        default=600.0,
        help="Max seconds to wait for background bash() commands in -p mode (0 to disable).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Always show full ipython code and output panels instead of compact summaries.",
    )
    args = parser.parse_args()

    cwd = args.cwd.resolve()
    max_context_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "128000"))
    compact_reserve_tokens = int(os.getenv("COMPACT_RESERVE_TOKENS", "16384"))
    keep_recent_messages = int(os.getenv("COMPACT_KEEP_RECENT_MESSAGES", "10"))

    client, model = _build_client()
    system_prompt = build_system_prompt(cwd)
    kernel = KernelClient(cwd=cwd)
    kernel.start()

    messages: list[dict[str, Any]] = []
    resumed = False
    restored_notice: str | None = None
    if args.cont or args.resume:
        target_id = args.resume or Session.latest_id(cwd=cwd)
        if not target_id:
            kernel.close()
            console.print(f"[red]Error: No previous session found for {cwd}.[/red]")
            sys.exit(1)
        try:
            if args.fork:
                active_session, messages = Session.fork(target_id, cwd)
            else:
                active_session, messages = Session.load(target_id)
        except FileNotFoundError as exc:
            kernel.close()
            console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        resumed = True
        restored_notice = active_session.restore_kernel(kernel)
        if restored_notice:
            messages.append({"role": "user", "content": restored_notice})
    else:
        active_session = Session.create(cwd)

    usage_stats = TurnUsage.from_dict(active_session.last_usage)

    def _persist(compacted: bool = False) -> None:
        active_session.save_messages(messages, compacted=compacted)
        if usage_stats.prompt_tokens > 0 or usage_stats.total_prompt_tokens > 0:
            active_session.save_usage(usage_stats.to_dict())
        active_session.save_kernel(kernel)

    cell_history: list[tuple[str, CellResult, float]] = []
    pending_cell_code: list[str] = []
    pending_cell_start: list[float] = []
    active_status: list[Status] = []
    streamed_any = [False]
    md_streamer = MarkdownBlockStreamer(lambda block: console.print(Markdown(block)))

    def _on_status(label: str | None) -> None:
        if label is None:
            if active_status:
                st = active_status.pop()
                st.__exit__(None, None, None)
            return
        if active_status:
            active_status[-1].update(label)
        else:
            st = console.status(label, spinner="dots")
            st.__enter__()
            active_status.append(st)

    def _on_text_delta(chunk: str) -> None:
        streamed_any[0] = True
        md_streamer.feed(chunk)

    def _on_tool_call(code: str) -> None:
        if streamed_any[0]:
            md_streamer.flush()
            streamed_any[0] = False
        pending_cell_code.append(code)
        pending_cell_start.append(time.monotonic())
        if args.verbose:
            _render_tool_call(code)

    def _on_tool_result(res: CellResult) -> None:
        code = pending_cell_code.pop() if pending_cell_code else ""
        started = pending_cell_start.pop() if pending_cell_start else time.monotonic()
        duration = time.monotonic() - started
        cell_history.append((code, res, duration))
        if args.verbose:
            _render_tool_result(res)
        else:
            summary = format_compact_cell_summary(len(cell_history), code, res, duration)
            style = "dim green" if res.ok else "red"
            console.print(f"[{style}]{summary}[/{style}]")

    def _run_turn(turn_usage: TurnUsage | None = None) -> tuple[str, list[dict[str, Any]], int]:
        streamed_any[0] = False
        try:
            answer, updated_msgs, tokens = agent_loop(
                client,
                model,
                system_prompt,
                messages,
                kernel,
                max_context_tokens=max_context_tokens,
                compact_reserve_tokens=compact_reserve_tokens,
                keep_recent_messages=keep_recent_messages,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                on_compaction=_render_compaction,
                on_bash_notice=_render_bash_notice,
                on_status=_on_status,
                on_text_delta=_on_text_delta,
                usage_stats=turn_usage,
                initial_prompt_tokens=turn_usage.prompt_tokens if turn_usage else 0,
            )
        finally:
            _on_status(None)
            md_streamer.flush()
        if not streamed_any[0] and answer:
            console.print(Markdown(answer))
        return answer, updated_msgs, tokens

    try:
        if args.prompt:
            messages.append({"role": "user", "content": args.prompt})
            _, messages, _ = _run_turn(turn_usage=usage_stats)
            _persist()

            while args.background_wait > 0 and (kernel.has_pending_events() or kernel.running_bash_count() > 0):
                events = kernel.wait_for_events(timeout=args.background_wait)
                if not events:
                    break
                notice = format_bash_done_events(events)
                if not notice:
                    continue
                _render_bash_notice(notice)
                messages.append({"role": "user", "content": notice})
                _, messages, _ = _run_turn(turn_usage=usage_stats)
                _persist()
            return

        console.print(
            Panel(
                f"Model: [bold]{model}[/bold] | Workspace: [bold]{cwd}[/bold] | Session: [bold]{active_session.id}[/bold]\n"
                "Commands: [cyan]/vars[/cyan], [cyan]/session[/cyan], [cyan]/usage[/cyan], "
                "[cyan]/expand[/cyan], [cyan]/compact[/cyan], [cyan]/reset[/cyan], [cyan]/exit[/cyan]\n"
                "[dim]Tip: Use @path for files, Esc+Enter or Ctrl+J for multi-line input, Ctrl+C to interrupt.[/dim]",
                title="[bold]quartz[/bold]",
                border_style="blue",
            )
        )

        if resumed:
            _replay_session(messages, cell_history, verbose=args.verbose)
            if usage_stats.prompt_tokens > 0:
                console.print(f"[dim]{_format_turn_footer(usage_stats, max_context_tokens)}[/dim]")

        kb = KeyBindings()

        @kb.add("c-j")
        def _(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("enter")
        def _(event: Any) -> None:
            buf = event.current_buffer
            if buf.text.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        history_path = Path.home() / ".quartz" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            completer=AgentCompleter(cwd),
            complete_while_typing=True,
            key_bindings=kb,
            prompt_continuation="... ",
        )

        while True:
            bg_count = kernel.running_bash_count()
            if kernel.has_pending_events():
                user_input = _WAKEUP_SENTINEL
            else:
                stop_watcher = threading.Event()
                watcher_thread: threading.Thread | None = None
                if bg_count > 0:
                    def _watch_wakeup() -> None:
                        while not stop_watcher.is_set():
                            with kernel._events_cond:
                                if not kernel._events:
                                    kernel._events_cond.wait(timeout=0.25)
                                if kernel._events:
                                    app = prompt_session.app
                                    if app.is_running:
                                        app.exit(result=_WAKEUP_SENTINEL)
                                    return

                    watcher_thread = threading.Thread(target=_watch_wakeup, daemon=True)
                    watcher_thread.start()

                try:
                    toolbar = _format_toolbar(model, active_session.id, usage_stats, max_context_tokens, bg_count)
                    user_input = prompt_session.prompt("\n> ", bottom_toolbar=toolbar).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Exiting.[/dim]")
                    break
                finally:
                    stop_watcher.set()
                    if watcher_thread is not None:
                        watcher_thread.join(timeout=0.5)

            if user_input == _WAKEUP_SENTINEL:
                events = kernel.drain_events()
                notice = format_bash_done_events(events)
                if not notice:
                    continue
                _render_bash_notice(notice)
                messages.append({"role": "user", "content": notice})
                try:
                    _, messages, tokens = _run_turn(turn_usage=usage_stats)
                    _persist()
                    if tokens:
                        console.print(f"[dim]{_format_turn_footer(usage_stats, max_context_tokens)}[/dim]")
                except KeyboardInterrupt:
                    kernel.interrupt()
                    console.print("\n[yellow]Interrupted.[/yellow]")
                except Exception as exc:
                    console.print(f"[red]Error: {exc}[/red]")
                continue

            if not user_input:
                continue

            if user_input in ("/exit", "/quit"):
                break
            if user_input == "/help":
                console.print(
                    "/vars            List live Python variables in the kernel\n"
                    "/session         Show active session ID and file paths\n"
                    "/usage           Show context window usage and cache hit rate\n"
                    "/expand [n]      Expand a collapsed ipython cell (default: latest)\n"
                    "/compact [focus] Summarize older messages while keeping the kernel alive\n"
                    "/prompt          Show the assembled system prompt\n"
                    "/reset           Clear conversation history, start a new session, and restart the kernel\n"
                    "/exit            Exit"
                )
                continue
            if user_input.startswith("/expand") or user_input == "/last":
                if not cell_history:
                    console.print("[dim]No ipython cells executed in this session yet.[/dim]")
                    continue
                arg = user_input[len("/expand") :].strip() if user_input.startswith("/expand") else ""
                try:
                    idx = int(arg) if arg else len(cell_history)
                    if not (1 <= idx <= len(cell_history)):
                        raise ValueError
                except ValueError:
                    console.print(f"[red]Invalid cell index. Choose 1..{len(cell_history)}.[/red]")
                    continue
                code, res, duration = cell_history[idx - 1]
                console.print(f"[dim]Cell #{idx} ({duration:.2f}s)[/dim]")
                _render_tool_call(code)
                _render_tool_result(res)
                continue
            if user_input == "/usage":
                pct = usage_stats.context_pct(max_context_tokens)
                cache_pct = usage_stats.cache_hit_rate * 100.0
                total_cache = (
                    (usage_stats.total_cached_tokens / usage_stats.total_prompt_tokens * 100.0)
                    if usage_stats.total_prompt_tokens > 0
                    else 0.0
                )
                console.print(
                    f"Last context:  {usage_stats.prompt_tokens:,} / {max_context_tokens:,} ({pct:.1f}%)\n"
                    f"Last cache:    {usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
                    f"Session total: {usage_stats.total_prompt_tokens:,} in / "
                    f"{usage_stats.total_completion_tokens:,} out / "
                    f"{usage_stats.total_cached_tokens:,} cached ({total_cache:.1f}%)"
                )
                continue
            if user_input == "/session":
                console.print(
                    f"Session ID: [bold]{active_session.id}[/bold]\n"
                    f"Log:        {active_session.jsonl_path}\n"
                    f"Snapshot:   {active_session.snapshot_dir}"
                )
                continue
            if user_input == "/vars":
                vars_info = kernel.list_vars()
                if not vars_info:
                    console.print("[dim]No user-defined variables in the kernel.[/dim]")
                else:
                    for name, typ in vars_info.items():
                        console.print(f"  [cyan]{name}[/cyan]: {typ}")
                continue
            if user_input == "/prompt":
                console.print(Panel(system_prompt, title="System Prompt", border_style="dim"))
                continue
            if user_input == "/reset":
                messages.clear()
                cell_history.clear()
                usage_stats = TurnUsage()
                kernel.restart()
                active_session = Session.create(cwd)
                console.print(
                    f"[yellow]Conversation cleared, kernel restarted, new session: {active_session.id}[/yellow]"
                )
                continue
            if user_input.startswith("/compact"):
                focus = user_input[len("/compact") :].strip() or None
                messages, summarized, kept = compact_messages(
                    client, model, messages, kernel, keep_recent=keep_recent_messages, focus=focus
                )
                if summarized > 0:
                    _persist(compacted=True)
                    _render_compaction(summarized, kept)
                else:
                    console.print("[dim]Nothing to compact yet.[/dim]")
                continue

            messages.append({"role": "user", "content": user_input})
            try:
                _, messages, tokens = _run_turn(turn_usage=usage_stats)
                _persist()
                if tokens:
                    console.print(f"[dim]{_format_turn_footer(usage_stats, max_context_tokens)}[/dim]")
            except KeyboardInterrupt:
                kernel.interrupt()
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as exc:
                console.print(f"[red]Error: {exc}[/red]")
    finally:
        if messages:
            try:
                active_session.save_kernel(kernel)
            except Exception:
                pass
        kernel.close()
