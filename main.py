"""CLI entrypoint for the minimal agent (interactive REPL + `-p` headless mode)."""

from __future__ import annotations

import argparse
import os
import sys
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

from agent.compact import compact_messages
from agent.kernel_client import CellResult, KernelClient
from agent.loop import agent_loop
from agent.prompt import build_system_prompt

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
    console.print(Panel(notice, title="[bold magenta]harness notice[/bold magenta]", border_style="magenta"))


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
            if answer:
                console.print(Markdown(answer))
            return

        console.print(
            Panel(
                f"Model: [bold]{model}[/bold] | Workspace: [bold]{cwd}[/bold]\n"
                "Commands: [cyan]/vars[/cyan], [cyan]/compact [focus][/cyan], "
                "[cyan]/prompt[/cyan], [cyan]/reset[/cyan], [cyan]/exit[/cyan]",
                title="[bold]Minimal Agent (learn-claude-code + Unharnessed)[/bold]",
                border_style="blue",
            )
        )

        session: PromptSession[str] = PromptSession(history=InMemoryHistory())

        while True:
            try:
                user_input = session.prompt("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Exiting.[/dim]")
                break

            if not user_input:
                continue

            if user_input in ("/exit", "/quit"):
                break
            if user_input == "/help":
                console.print(
                    "/vars            List live Python variables in the kernel\n"
                    "/compact [focus] Summarize older messages while keeping the kernel alive\n"
                    "/prompt          Show the assembled system prompt\n"
                    "/reset           Clear conversation history and restart the kernel\n"
                    "/exit            Exit"
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
                console.print("[yellow]Conversation cleared and Python kernel restarted.[/yellow]")
                continue
            if user_input.startswith("/compact"):
                focus = user_input[len("/compact") :].strip() or None
                messages, summarized, kept = compact_messages(
                    client, model, messages, kernel, keep_recent=keep_recent_messages, focus=focus
                )
                if summarized > 0:
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
                )
                if answer:
                    console.print(Markdown(answer))
                if tokens:
                    console.print(f"[dim](prompt tokens: {tokens})[/dim]")
            except KeyboardInterrupt:
                kernel.interrupt()
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as exc:
                console.print(f"[red]Error: {exc}[/red]")
    finally:
        kernel.close()


if __name__ == "__main__":
    main()
