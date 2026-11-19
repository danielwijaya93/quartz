"""CLI entrypoint for quartz (interactive REPL, `-p` headless mode, and `mcp` subcommands)."""

from __future__ import annotations

import argparse
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.markup import escape
from rich.status import Status
from rich.text import Text

from agent.actions import describe_cell_actions
from agent.commands import (
    _CLIRuntime,
    _build_client,
    _handle_auth_cli,
    _handle_mcp_cli,
    _handle_slash_command,
    _parse_kv_pairs,
)
from agent.completer import AgentCompleter
from agent.images import (
    ImageAttachment,
    build_user_content,
    extract_inline_image_mentions,
    load_image_file,
)
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnAborted, TurnUsage, agent_loop, format_bash_done_events
from agent.prompt import build_system_prompt
from agent.session import Session
from agent.ui import (
    BlockPrinter,
    CellGroupBuffer,
    MarkdownBlockStreamer,
    _MidTurnInputWatcher,
    _fmt_duration,
    _fmt_k,
    _fmt_stopwatch,
    _fmt_thought_duration,
    _format_toolbar,
    _format_turn_footer,
    _format_turn_timing,
    _parse_expand_range,
    _pick_option,
    _render_bash_notice,
    _render_compaction,
    _render_gutter_block,
    _render_tool_call,
    _render_tool_result,
    _replay_session,
    _short_path,
    console,
    format_compact_cell_summary,
    format_grouped_cell_summary,
    printer,
    render_markdown_block,
)

_WAKEUP_SENTINEL = "__MINIMAL_AGENT_WAKEUP__"

__all__ = [
    "BlockPrinter",
    "CellGroupBuffer",
    "MarkdownBlockStreamer",
    "_CLIRuntime",
    "_MidTurnInputWatcher",
    "_build_client",
    "_fmt_duration",
    "_fmt_k",
    "_fmt_stopwatch",
    "_fmt_thought_duration",
    "_format_toolbar",
    "_format_turn_footer",
    "_format_turn_timing",
    "_handle_auth_cli",
    "_handle_mcp_cli",
    "_handle_slash_command",
    "_parse_expand_range",
    "_parse_kv_pairs",
    "_pick_option",
    "_render_bash_notice",
    "_render_compaction",
    "_render_gutter_block",
    "_render_tool_call",
    "_render_tool_result",
    "_replay_session",
    "_short_path",
    "console",
    "format_compact_cell_summary",
    "format_grouped_cell_summary",
    "main",
    "printer",
    "render_markdown_block",
]


def _run_turn(
    rt: _CLIRuntime,
    *,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    on_steering: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    pending_cell_code: list[str] = []
    pending_cell_start: list[float] = []
    active_status: list[Status] = []
    status_lock = threading.Lock()
    thinking_started: list[float | None] = [None]
    thinking_stop: list[threading.Event | None] = [None]
    streak_thought_s = [0.0]
    has_streak_thought = [False]
    streamed_any = [False]
    md_streamer = MarkdownBlockStreamer(printer.markdown)
    cell_group = CellGroupBuffer()

    def _flush_streak() -> None:
        if has_streak_thought[0]:
            elapsed = streak_thought_s[0]
            streak_thought_s[0] = 0.0
            has_streak_thought[0] = False
            printer.block(Text(f"Thought for {_fmt_thought_duration(elapsed)}", style="dim"))
        cell_group.flush()

    def _tick_thinking(started: float, stop_event: threading.Event) -> None:
        while not stop_event.wait(0.2):
            label = f"Thinking... {_fmt_stopwatch(time.monotonic() - started)}"
            with status_lock:
                if stop_event.is_set() or not active_status:
                    return
                active_status[-1].update(label)

    def _stop_thinking() -> float | None:
        with status_lock:
            if thinking_stop[0] is not None:
                thinking_stop[0].set()
                thinking_stop[0] = None
            started = thinking_started[0]
            thinking_started[0] = None
            if started is not None and active_status:
                active_status.pop().__exit__(None, None, None)
        if started is not None:
            elapsed = time.monotonic() - started
            streak_thought_s[0] += elapsed
            has_streak_thought[0] = True
            return elapsed
        return None

    def _on_status(label: str | None) -> None:
        if rt.plain_output:
            return
        if label is None:
            _stop_thinking()
            with status_lock:
                if active_status:
                    active_status.pop().__exit__(None, None, None)
            return
        if label == "Thinking...":
            with status_lock:
                if thinking_started[0] is not None:
                    return
                started = time.monotonic()
                thinking_started[0] = started
                stop_event = threading.Event()
                thinking_stop[0] = stop_event
                tick_label = f"Thinking... {_fmt_stopwatch(0.0)}"
                if active_status:
                    active_status[-1].update(tick_label)
                else:
                    st = console.status(tick_label, spinner="dots")
                    st.__enter__()
                    active_status.append(st)
            threading.Thread(target=_tick_thinking, args=(started, stop_event), daemon=True).start()
            return
        _stop_thinking()
        if label == "Running ipython...":
            action = describe_cell_actions(pending_cell_code[-1]) if pending_cell_code else "Run Python"
            if cell_group.entries:
                label = f"{action} #{len(rt.cell_history) + 1} ({len(cell_group.entries)} done)..."
            else:
                label = f"{action}..."
        with status_lock:
            if active_status:
                active_status[-1].update(label)
            else:
                st = console.status(label, spinner="dots")
                st.__enter__()
                active_status.append(st)

    def _on_text_delta(chunk: str) -> None:
        _flush_streak()
        streamed_any[0] = True
        if rt.plain_output:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        else:
            md_streamer.feed(chunk)

    def _on_tool_call(code: str) -> None:
        if streamed_any[0]:
            if rt.plain_output:
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                md_streamer.flush()
            streamed_any[0] = False
        pending_cell_code.append(code)
        pending_cell_start.append(time.monotonic())
        if rt.verbose:
            _flush_streak()
            _render_tool_call(code)

    def _on_tool_result(res: CellResult) -> None:
        code = pending_cell_code.pop(0) if pending_cell_code else ""
        started = pending_cell_start.pop(0) if pending_cell_start else time.monotonic()
        duration = time.monotonic() - started
        rt.cell_history.append((code, res, duration))
        if rt.verbose:
            _render_tool_result(res)
        else:
            cell_group.add(len(rt.cell_history), code, res, duration)

    streamed_any[0] = False
    printer.reset()
    try:
        answer, updated_msgs, tokens = agent_loop(
            rt.client,
            rt.model,
            rt.system_prompt,
            rt.messages,
            rt.kernel,
            max_context_tokens=rt.max_context_tokens,
            compact_reserve_tokens=rt.compact_reserve_tokens,
            keep_recent_messages=rt.keep_recent_messages,
            on_tool_call=_on_tool_call,
            on_tool_result=_on_tool_result,
            on_compaction=lambda s, k: (_flush_streak(), _render_compaction(s, k)),
            on_bash_notice=lambda n: (_flush_streak(), _render_bash_notice(n)),
            on_status=_on_status,
            on_text_delta=_on_text_delta,
            usage_stats=rt.usage_stats,
            initial_prompt_tokens=rt.usage_stats.prompt_tokens,
            effort=rt.effort,
            abort_event=abort_event,
            steering_queue=steering_queue,
            on_steering=on_steering,
        )
    finally:
        _on_status(None)
        _flush_streak()
        if rt.plain_output:
            if streamed_any[0]:
                sys.stdout.write("\n")
                sys.stdout.flush()
        else:
            md_streamer.flush()
    if not streamed_any[0] and answer:
        if rt.plain_output:
            sys.stdout.write(answer.rstrip() + "\n")
            sys.stdout.flush()
        else:
            printer.markdown(answer)
    return answer, updated_msgs, tokens


def _run_headless(
    rt: _CLIRuntime, prompt: str, image_args: list[str], background_wait: float
) -> None:
    cli_images: list[ImageAttachment] = []
    for raw_img in image_args:
        img_path = Path(raw_img).expanduser()
        if not img_path.is_absolute():
            img_path = rt.cwd / img_path
        try:
            cli_images.append(load_image_file(img_path))
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            return
    cli_images.extend(extract_inline_image_mentions(prompt, rt.cwd))
    rt.messages.append({"role": "user", "content": build_user_content(prompt, cli_images)})
    _, rt.messages, _ = _run_turn(rt)
    rt.persist()

    while background_wait > 0 and (
        rt.kernel.has_pending_events() or rt.kernel.running_bash_count() > 0
    ):
        events = rt.kernel.wait_for_events(timeout=background_wait)
        if not events:
            break
        notice = format_bash_done_events(events)
        if not notice:
            continue
        _render_bash_notice(notice)
        rt.messages.append({"role": "user", "content": notice})
        _, rt.messages, _ = _run_turn(rt)
        rt.persist()


def _execute_interactive_turn(rt: _CLIRuntime) -> None:
    abort_event = threading.Event()
    steering_queue: queue.Queue[str] = queue.Queue()

    def _on_queued(text: str) -> None:
        preview = text[:60] + ("..." if len(text) > 60 else "")
        printer.block(Text(f"[queued] {preview}", style="dim cyan"))

    def _on_steering(text: str) -> None:
        preview = text[:60] + ("..." if len(text) > 60 else "")
        printer.block(Text(f"[steering] {preview}", style="cyan"))

    t0 = time.monotonic()
    with _MidTurnInputWatcher(
        abort_event,
        steering_queue,
        on_abort=rt.kernel.interrupt,
        on_queued=_on_queued,
    ):
        _, rt.messages, tokens = _run_turn(
            rt,
            abort_event=abort_event,
            steering_queue=steering_queue,
            on_steering=_on_steering,
        )
    turn_elapsed = time.monotonic() - t0
    rt.total_elapsed_s += turn_elapsed
    rt.turn_count += 1
    rt.persist()
    timing = _format_turn_timing(turn_elapsed, rt.total_elapsed_s, rt.turn_count)
    if tokens:
        footer = f"{timing} · {_format_turn_footer(rt.usage_stats, rt.max_context_tokens)}"
    else:
        footer = timing
    printer.block(Text(footer, style="dim"))


def _run_interactive(rt: _CLIRuntime, resumed: bool) -> None:
    console.print(Text.assemble(("quartz", "bold"), (" v0.1.0 · Run /help for commands", "dim")))

    if resumed:
        _replay_session(rt.messages, rt.cell_history, verbose=rt.verbose)
        if rt.usage_stats.prompt_tokens > 0:
            printer.block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))

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
    prompt_style = Style.from_dict({"bottom-toolbar": "noreverse ansibrightblack"})
    prompt_session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_path)),
        completer=AgentCompleter(rt.cwd),
        complete_while_typing=True,
        key_bindings=kb,
        prompt_continuation="▌ ",
        style=prompt_style,
    )

    while True:
        bg_count = rt.kernel.running_bash_count()
        if rt.kernel.has_pending_events():
            user_input = _WAKEUP_SENTINEL
        else:
            stop_watcher = threading.Event()
            watcher_thread: threading.Thread | None = None
            if bg_count > 0:
                def _watch_wakeup() -> None:
                    if rt.kernel.wait_until_event(stop_watcher, timeout=0.25):
                        app = prompt_session.app
                        if app.is_running:
                            app.exit(result=_WAKEUP_SENTINEL)

                watcher_thread = threading.Thread(target=_watch_wakeup, daemon=True)
                watcher_thread.start()

            try:
                toolbar = _format_toolbar(
                    rt.model, rt.effort, rt.cwd, rt.usage_stats, rt.max_context_tokens, bg_count
                )
                user_input = prompt_session.prompt("\n▌ ", bottom_toolbar=toolbar).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Exiting.[/dim]")
                break
            finally:
                stop_watcher.set()
                if watcher_thread is not None:
                    watcher_thread.join(timeout=0.5)

        if user_input == _WAKEUP_SENTINEL:
            events = rt.kernel.drain_events()
            notice = format_bash_done_events(events)
            if not notice:
                continue
            _render_bash_notice(notice)
            rt.messages.append({"role": "user", "content": notice})
        else:
            if not user_input:
                continue
            cmd_res = _handle_slash_command(rt, user_input)
            if cmd_res is True:
                break
            if cmd_res is False:
                continue
            inline_images = extract_inline_image_mentions(user_input, rt.cwd)
            attached_images = [*rt.pending_images, *inline_images]
            rt.pending_images.clear()
            rt.messages.append({"role": "user", "content": build_user_content(user_input, attached_images)})

        try:
            _execute_interactive_turn(rt)
        except TurnAborted:
            rt.kernel.interrupt()
            rt.persist()
            console.print("\n[yellow]Aborted (Esc).[/yellow]")
        except KeyboardInterrupt:
            rt.kernel.interrupt()
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")


def _build_runtime(args: argparse.Namespace) -> tuple[_CLIRuntime, bool]:
    cwd = args.cwd.resolve()
    max_context_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "128000"))
    compact_reserve_tokens = int(os.getenv("COMPACT_RESERVE_TOKENS", "16384"))
    keep_recent_messages = int(os.getenv("COMPACT_KEEP_RECENT_MESSAGES", "10"))

    kernel = KernelClient(cwd=cwd)
    kernel_err: list[BaseException] = []

    def _start_kernel() -> None:
        try:
            kernel.start()
        except BaseException as exc:
            kernel_err.append(exc)

    kernel_thread = threading.Thread(target=_start_kernel, daemon=True)
    kernel_thread.start()
    try:
        client, provider_name, model, effort = _build_client(
            provider=args.provider, model_override=args.model, effort_override=args.effort
        )
        system_prompt = build_system_prompt(cwd, headless=bool(args.prompt))
    finally:
        kernel_thread.join()
    if kernel_err:
        raise kernel_err[0]

    messages: list[dict[str, Any]] = []
    resumed = False
    if args.cont or args.resume:
        target_id = args.resume or Session.latest_id(cwd=cwd)
        if not target_id:
            kernel.close()
            console.print(f"[red]Error: No previous session found for {escape(_short_path(cwd))}.[/red]")
            sys.exit(1)
        try:
            if args.fork:
                active_session, messages = Session.fork(target_id, cwd)
            else:
                active_session, messages = Session.load(target_id)
        except FileNotFoundError as exc:
            kernel.close()
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            sys.exit(1)
        resumed = True
        restored_notice = active_session.restore_kernel(kernel)
        if restored_notice:
            messages.append({"role": "user", "content": restored_notice})
    else:
        active_session = Session.create(cwd)

    rt = _CLIRuntime(
        cwd=cwd,
        kernel=kernel,
        client=client,
        provider_name=provider_name,
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        active_session=active_session,
        messages=messages,
        usage_stats=TurnUsage.from_dict(active_session.last_usage),
        max_context_tokens=max_context_tokens,
        compact_reserve_tokens=compact_reserve_tokens,
        keep_recent_messages=keep_recent_messages,
        verbose=args.verbose,
        plain_output=bool(args.prompt) or not sys.stdout.isatty(),
        headless=bool(args.prompt),
    )
    return rt, resumed


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        _handle_mcp_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _handle_auth_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "acp":
        sys.argv[1] = "--acp"

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        pass

    quartz_env = Path.home() / ".quartz" / ".env"
    if quartz_env.is_file():
        load_dotenv(quartz_env, override=False)
    load_dotenv(override=False)

    parser = argparse.ArgumentParser(description="quartz — minimal coding agent with a persistent Python REPL.")
    parser.add_argument("-p", "--prompt", help="Run a single prompt in headless mode and exit.")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Attach an image file to the -p prompt (repeatable).",
    )
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
        "--provider",
        choices=["auto", "deepseek", "google", "openai"],
        default=os.getenv("PROVIDER", "auto"),
        help="LLM provider backend (default: auto).",
    )
    parser.add_argument("--model", help="Override MODEL from environment.")
    parser.add_argument("--effort", help="Reasoning effort level (e.g. low, medium, high, max, xhigh).")
    parser.add_argument(
        "--acp",
        action="store_true",
        help="Run as an ACP (Agent Client Protocol) JSON-RPC 2.0 server over stdio.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Always show full ipython code and output panels instead of compact summaries.",
    )
    args = parser.parse_args()

    if args.acp:
        from agent.acp import ACPServer

        server = ACPServer(
            lambda: _build_client(
                provider=args.provider, model_override=args.model, effort_override=args.effort
            ),
            max_context_tokens=int(os.getenv("MAX_CONTEXT_TOKENS", "128000")),
            compact_reserve_tokens=int(os.getenv("COMPACT_RESERVE_TOKENS", "16384")),
            keep_recent_messages=int(os.getenv("COMPACT_KEEP_RECENT_MESSAGES", "10")),
        )
        server.serve()
        return

    rt, resumed = _build_runtime(args)
    try:
        if args.prompt:
            _run_headless(rt, args.prompt, args.image, args.background_wait)
        else:
            _run_interactive(rt, resumed)
    finally:
        if rt.messages and not rt.headless:
            try:
                rt.active_session.save_kernel(rt.kernel)
            except Exception:
                pass
        rt.kernel.close()
