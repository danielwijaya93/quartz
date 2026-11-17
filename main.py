"""CLI entrypoint for the minimal agent (interactive REPL + `-p` headless mode)."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
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
from agent.prompt import build_system_prompt
from agent.session import Session

_WAKEUP_SENTINEL = "__MINIMAL_AGENT_WAKEUP__"

console = Console()


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
    code_preview = (code_lines[0][:50] + "...") if code_lines and len(code_lines[0]) > 50 else (code_lines[0] if code_lines else "")
    out_text = res.format_for_model().strip()
    out_lines = out_text.splitlines() if out_text and out_text != "(ok, no output)" else []

    if res.ok:
        out_summary = f"{len(out_lines)} lines out" if len(out_lines) > 1 else (out_lines[0][:50] if out_lines else "no output")
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
        console.print("[red]Error: OPENAI_API_KEY is not set. Copy .env.example to .env and configure it.[/red]")
        sys.exit(1)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("MODEL", "gpt-4.1")
    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Minimal agent with a single persistent Python REPL tool.")
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
    if args.cont or args.resume:
        target_id = args.resume or Session.latest_id()
        if not target_id:
            kernel.close()
            console.print("[red]Error: No previous session found to resume.[/red]")
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

        restored_notice = active_session.restore_kernel(kernel)
        if restored_notice:
            messages.append({"role": "user", "content": restored_notice})
            if not args.prompt:
                console.print(Panel(restored_notice, title="[bold cyan]session restored[/bold cyan]", border_style="cyan"))
    else:
        active_session = Session.create(cwd)

    def _persist(compacted: bool = False) -> None:
        active_session.save_messages(messages, compacted=compacted)
        active_session.save_kernel(kernel)

    cell_history: list[tuple[str, CellResult, float]] = []
    pending_cell_code: list[str] = []
    pending_cell_start: list[float] = []
    active_status: list[Status] = []
    streamed_any = [False]

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
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def _on_tool_call(code: str) -> None:
        if streamed_any[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
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

    def _run_turn(usage_stats: TurnUsage | None = None) -> tuple[str, list[dict[str, Any]], int]:
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
                usage_stats=usage_stats,
            )
        finally:
            _on_status(None)
        if streamed_any[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif answer:
            console.print(Markdown(answer))
        return answer, updated_msgs, tokens

    try:
        if args.prompt:
            messages.append({"role": "user", "content": args.prompt})
            _, messages, _ = _run_turn()
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
                _, messages, _ = _run_turn()
                _persist()
            return

        console.print(
            Panel(
                f"Model: [bold]{model}[/bold] | Workspace: [bold]{cwd}[/bold] | Session: [bold]{active_session.id}[/bold]\n"
                "Commands: [cyan]/vars[/cyan], [cyan]/session[/cyan], [cyan]/usage[/cyan], "
                "[cyan]/expand[/cyan], [cyan]/compact[/cyan], [cyan]/reset[/cyan], [cyan]/exit[/cyan]\n"
                "[dim]Tip: Use @path for files, Esc+Enter or Ctrl+J for multi-line input, Ctrl+C to interrupt.[/dim]",
                title="[bold]Minimal Agent (learn-claude-code + Unharnessed)[/bold]",
                border_style="blue",
            )
        )

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

        history_path = Path.home() / ".minimal-agent" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            completer=AgentCompleter(cwd),
            complete_while_typing=True,
            key_bindings=kb,
            prompt_continuation="... ",
        )
        usage_stats = TurnUsage()

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
                    _, messages, tokens = _run_turn(usage_stats=usage_stats)
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
                _, messages, tokens = _run_turn(usage_stats=usage_stats)
                _persist()
                if tokens:
                    console.print(f"[dim]{_format_turn_footer(usage_stats, max_context_tokens)}[/dim]")
            except KeyboardInterrupt:
                kernel.interrupt()
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as exc:
                console.print(f"[red]Error: {exc}[/red]")
    finally:
        try:
            active_session.save_kernel(kernel)
        except Exception:
            pass
        kernel.close()


if __name__ == "__main__":
    main()
