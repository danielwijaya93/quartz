"""CLI entrypoint for the minimal agent (interactive REPL + `-p` headless mode)."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from agent.compact import compact_messages
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

    try:
        if args.prompt:
            messages.append({"role": "user", "content": args.prompt})
            answer, messages, _ = agent_loop(
                client,
                model,
                system_prompt,
                messages,
                kernel,
                max_context_tokens=max_context_tokens,
                compact_reserve_tokens=compact_reserve_tokens,
                keep_recent_messages=keep_recent_messages,
                on_tool_call=_render_tool_call,
                on_tool_result=_render_tool_result,
                on_compaction=_render_compaction,
                on_bash_notice=_render_bash_notice,
            )
            _persist()
            if answer:
                console.print(Markdown(answer))

            while args.background_wait > 0 and (kernel.has_pending_events() or kernel.running_bash_count() > 0):
                events = kernel.wait_for_events(timeout=args.background_wait)
                if not events:
                    break
                notice = format_bash_done_events(events)
                if not notice:
                    continue
                _render_bash_notice(notice)
                messages.append({"role": "user", "content": notice})
                answer, messages, _ = agent_loop(
                    client,
                    model,
                    system_prompt,
                    messages,
                    kernel,
                    max_context_tokens=max_context_tokens,
                    compact_reserve_tokens=compact_reserve_tokens,
                    keep_recent_messages=keep_recent_messages,
                    on_tool_call=_render_tool_call,
                    on_tool_result=_render_tool_result,
                    on_compaction=_render_compaction,
                    on_bash_notice=_render_bash_notice,
                )
                _persist()
                if answer:
                    console.print(Markdown(answer))
            return

        console.print(
            Panel(
                f"Model: [bold]{model}[/bold] | Workspace: [bold]{cwd}[/bold] | Session: [bold]{active_session.id}[/bold]\n"
                "Commands: [cyan]/vars[/cyan], [cyan]/session[/cyan], [cyan]/compact [focus][/cyan], "
                "[cyan]/prompt[/cyan], [cyan]/reset[/cyan], [cyan]/exit[/cyan]",
                title="[bold]Minimal Agent (learn-claude-code + Unharnessed)[/bold]",
                border_style="blue",
            )
        )

        prompt_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
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
                    answer, messages, tokens = agent_loop(
                        client,
                        model,
                        system_prompt,
                        messages,
                        kernel,
                        max_context_tokens=max_context_tokens,
                        compact_reserve_tokens=compact_reserve_tokens,
                        keep_recent_messages=keep_recent_messages,
                        on_tool_call=_render_tool_call,
                        on_tool_result=_render_tool_result,
                        on_compaction=_render_compaction,
                        on_bash_notice=_render_bash_notice,
                        usage_stats=usage_stats,
                    )
                    _persist()
                    if answer:
                        console.print(Markdown(answer))
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
                    "/compact [focus] Summarize older messages while keeping the kernel alive\n"
                    "/prompt          Show the assembled system prompt\n"
                    "/reset           Clear conversation history, start a new session, and restart the kernel\n"
                    "/exit            Exit"
                )
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
                answer, messages, tokens = agent_loop(
                    client,
                    model,
                    system_prompt,
                    messages,
                    kernel,
                    max_context_tokens=max_context_tokens,
                    compact_reserve_tokens=compact_reserve_tokens,
                    keep_recent_messages=keep_recent_messages,
                    on_tool_call=_render_tool_call,
                    on_tool_result=_render_tool_result,
                    on_compaction=_render_compaction,
                    on_bash_notice=_render_bash_notice,
                    usage_stats=usage_stats,
                )
                _persist()
                if answer:
                    console.print(Markdown(answer))
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
