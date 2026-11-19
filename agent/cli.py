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
from typing import TYPE_CHECKING, Any

from agent.compact import DEFAULT_KEEP_RECENT_TOKENS
from agent.models import PROVIDERS

if TYPE_CHECKING:
    from rich.status import Status

    from agent.commands import _CLIRuntime
    from agent.kernel_client import CellResult
    from agent.ui import (
        BlockPrinter,
        CellGroupBuffer,
        MarkdownBlockStreamer,
        TurnProgressTracker,
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

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BlockPrinter": ("agent.ui", "BlockPrinter"),
    "CellGroupBuffer": ("agent.ui", "CellGroupBuffer"),
    "MarkdownBlockStreamer": ("agent.ui", "MarkdownBlockStreamer"),
    "_CLIRuntime": ("agent.commands", "_CLIRuntime"),
    "_MidTurnInputWatcher": ("agent.ui", "_MidTurnInputWatcher"),
    "_build_client": ("agent.commands", "_build_client"),
    "_fmt_duration": ("agent.ui", "_fmt_duration"),
    "_fmt_k": ("agent.ui", "_fmt_k"),
    "_fmt_stopwatch": ("agent.ui", "_fmt_stopwatch"),
    "_fmt_thought_duration": ("agent.ui", "_fmt_thought_duration"),
    "_format_toolbar": ("agent.ui", "_format_toolbar"),
    "_format_turn_footer": ("agent.ui", "_format_turn_footer"),
    "_format_turn_timing": ("agent.ui", "_format_turn_timing"),
    "_handle_auth_cli": ("agent.commands", "_handle_auth_cli"),
    "_handle_mcp_cli": ("agent.commands", "_handle_mcp_cli"),
    "_handle_slash_command": ("agent.commands", "_handle_slash_command"),
    "_parse_expand_range": ("agent.ui", "_parse_expand_range"),
    "_parse_kv_pairs": ("agent.commands", "_parse_kv_pairs"),
    "_pick_option": ("agent.ui", "_pick_option"),
    "_render_bash_notice": ("agent.ui", "_render_bash_notice"),
    "_render_compaction": ("agent.ui", "_render_compaction"),
    "_render_gutter_block": ("agent.ui", "_render_gutter_block"),
    "_render_tool_call": ("agent.ui", "_render_tool_call"),
    "_render_tool_result": ("agent.ui", "_render_tool_result"),
    "_replay_session": ("agent.ui", "_replay_session"),
    "_short_path": ("agent.ui", "_short_path"),
    "TurnProgressTracker": ("agent.ui", "TurnProgressTracker"),
    "agent_loop": ("agent.loop", "agent_loop"),
    "console": ("agent.ui", "console"),
    "format_compact_cell_summary": ("agent.ui", "format_compact_cell_summary"),
    "format_grouped_cell_summary": ("agent.ui", "format_grouped_cell_summary"),
    "printer": ("agent.ui", "printer"),
    "render_markdown_block": ("agent.ui", "render_markdown_block"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    import importlib

    mod = importlib.import_module(mod_name)
    val = getattr(mod, attr_name)
    globals()[name] = val
    return val


def _sym(name: str) -> Any:
    """Resolve a lazily-exported symbol, respecting any monkeypatch in globals()."""
    val = globals().get(name)
    if val is not None:
        return val
    return __getattr__(name)

__all__ = [
    "BlockPrinter",
    "CellGroupBuffer",
    "MarkdownBlockStreamer",
    "TurnProgressTracker",
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
    tracker: TurnProgressTracker | None = None,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    on_steering: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    from agent.ui import (
        MarkdownBlockStreamer,
        TurnProgressTracker,
        _format_toolbar,
        _render_bash_notice,
        _render_compaction,
        _render_tool_call,
        _render_tool_result,
    )

    pending_cell_code: list[str] = []
    pending_cell_start: list[float] = []
    streamed_any = [False]
    active_printer = _sym("printer")
    active_console = _sym("console")
    md_streamer = MarkdownBlockStreamer(active_printer.markdown)
    own_tracker = tracker is None
    if tracker is None:
        tracker = _sym("TurnProgressTracker")(
            active_console,
            active_printer,
            verbose=rt.verbose,
            plain_output=rt.plain_output,
            toolbar_provider=lambda: _format_toolbar(
                rt.model,
                rt.effort,
                rt.cwd,
                rt.usage_stats,
                rt.max_context_tokens,
                getattr(rt.kernel, "running_bash_count", lambda: 0)(),
            ),
        )
    turn_stop = threading.Event()

    def _tick_tracker() -> None:
        while not turn_stop.wait(0.1):
            tracker.tick()

    if not rt.plain_output and not rt.verbose:
        threading.Thread(target=_tick_tracker, daemon=True).start()

    def _on_status(label: str | None) -> None:
        if rt.plain_output:
            return
        if label == "Thinking...":
            tracker.start_thinking()
        elif label is None:
            tracker.stop_thinking()

    def _on_text_delta(chunk: str) -> None:
        tracker.flush()
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
            tracker.flush()
            _render_tool_call(code)
        else:
            tracker.start_tool(code)

    def _on_tool_result(res: CellResult) -> None:
        code = pending_cell_code.pop(0) if pending_cell_code else ""
        started = pending_cell_start.pop(0) if pending_cell_start else time.monotonic()
        duration = time.monotonic() - started
        rt.cell_history.append((code, res, duration))
        if rt.verbose:
            _render_tool_result(res)
        else:
            tracker.add_tool_result(len(rt.cell_history), code, res, duration)

    streamed_any[0] = False
    active_printer.reset()
    try:
        answer, updated_msgs, tokens = _sym("agent_loop")(
            rt.client,
            rt.model,
            rt.system_prompt,
            rt.messages,
            rt.kernel,
            max_context_tokens=rt.max_context_tokens,
            compact_reserve_tokens=rt.compact_reserve_tokens,
            keep_recent_tokens=rt.keep_recent_tokens,
            on_tool_call=_on_tool_call,
            on_tool_result=_on_tool_result,
            on_compaction=lambda result: (tracker.flush(), _render_compaction(result)),
            on_bash_notice=lambda n: (tracker.flush(), _render_bash_notice(n)),
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
        turn_stop.set()
        if own_tracker and tracker is not None:
            tracker.close()
        elif tracker is not None:
            tracker.flush()
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
            active_printer.markdown(answer)
    return answer, updated_msgs, tokens


def _run_headless(
    rt: _CLIRuntime, prompt: str, image_args: list[str], background_wait: float
) -> None:
    from rich.markup import escape

    from agent.images import (
        ImageAttachment,
        build_user_content,
        extract_inline_image_mentions,
        load_image_file,
    )
    from agent.loop import format_bash_done_events
    from agent.ui import _render_bash_notice

    active_console = _sym("console")
    cli_images: list[ImageAttachment] = []
    for raw_img in image_args:
        img_path = Path(raw_img).expanduser()
        if not img_path.is_absolute():
            img_path = rt.cwd / img_path
        try:
            cli_images.append(load_image_file(img_path))
        except (FileNotFoundError, ValueError) as exc:
            active_console.print(f"[red]Error: {escape(str(exc))}[/red]")
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


def _execute_interactive_turn(rt: _CLIRuntime) -> str:
    from rich.text import Text

    from agent.ui import (
        TurnProgressTracker,
        _MidTurnInputWatcher,
        _format_toolbar,
        _format_turn_footer,
        _format_turn_timing,
    )

    active_printer = _sym("printer")
    active_console = _sym("console")
    abort_event = threading.Event()
    steering_queue: queue.Queue[str] = queue.Queue()

    tracker: TurnProgressTracker = _sym("TurnProgressTracker")(
        active_console,
        active_printer,
        verbose=rt.verbose,
        plain_output=rt.plain_output,
        toolbar_provider=lambda: _format_toolbar(
            rt.model,
            rt.effort,
            rt.cwd,
            rt.usage_stats,
            rt.max_context_tokens,
            getattr(rt.kernel, "running_bash_count", lambda: 0)(),
        ),
    )

    def _on_change(text: str) -> None:
        tracker.set_input_buffer(text)

    def _on_queued(text: str) -> None:
        tracker.add_queued_message(text)
        if rt.plain_output or rt.verbose:
            preview = text[:60] + ("..." if len(text) > 60 else "")
            active_printer.block(Text(f"[queued] {preview}", style="dim cyan"))

    def _on_steering(text: str) -> None:
        tracker.clear_queued_messages()
        preview = text[:60] + ("..." if len(text) > 60 else "")
        active_printer.block(Text(f"[steering] {preview}", style="cyan"))

    t0 = time.monotonic()
    leftover_buffer = ""
    watcher_cls = _sym("_MidTurnInputWatcher")
    try:
        with watcher_cls(
            abort_event,
            steering_queue,
            on_abort=rt.kernel.interrupt,
            on_queued=_on_queued,
            on_change=_on_change,
        ) as watcher:
            _, rt.messages, tokens = _run_turn(
                rt,
                tracker=tracker,
                abort_event=abort_event,
                steering_queue=steering_queue,
                on_steering=_on_steering,
            )
            leftover_buffer = watcher.current_buffer()
    finally:
        tracker.close()

    turn_elapsed = time.monotonic() - t0
    rt.total_elapsed_s += turn_elapsed
    rt.turn_count += 1
    rt.persist()
    timing = _format_turn_timing(turn_elapsed, rt.total_elapsed_s, rt.turn_count)
    if tokens:
        footer = f"{timing} · {_format_turn_footer(rt.usage_stats, rt.max_context_tokens)}"
    else:
        footer = timing
    active_printer.block(Text(footer, style="dim"))
    return leftover_buffer


def _run_interactive(rt: _CLIRuntime, resumed: bool) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from rich.markup import escape
    from rich.text import Text

    from agent.commands import _handle_slash_command
    from agent.completer import AgentCompleter
    from agent.images import build_user_content, extract_inline_image_mentions
    from agent.loop import TurnAborted, format_bash_done_events
    from agent.ui import (
        _format_toolbar,
        _format_turn_footer,
        _render_bash_notice,
        _replay_session,
    )

    active_console = _sym("console")
    active_printer = _sym("printer")
    active_console.print(Text.assemble(("quartz", "bold"), (" v0.1.0 · Run /help for commands", "dim")))

    if resumed:
        _replay_session(rt.messages, rt.cell_history, verbose=rt.verbose)
        if rt.usage_stats.prompt_tokens > 0:
            active_printer.block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))

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

    leftover_input = ""
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

            prompt_default = leftover_input
            leftover_input = ""
            try:
                toolbar = _format_toolbar(
                    rt.model, rt.effort, rt.cwd, rt.usage_stats, rt.max_context_tokens, bg_count
                )
                user_input = prompt_session.prompt(
                    "\n▌ ",
                    bottom_toolbar=toolbar,
                    default=prompt_default,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                active_console.print("\n[dim]Exiting.[/dim]")
                break
            finally:
                stop_watcher.set()
                if watcher_thread is not None:
                    watcher_thread.join(timeout=0.5)

        if user_input == _WAKEUP_SENTINEL:
            leftover_input = prompt_default
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
            if not rt.plain_output:
                active_console.print()

        try:
            leftover_input = _execute_interactive_turn(rt)
        except TurnAborted:
            rt.kernel.interrupt()
            rt.persist()
            active_console.print("\n[yellow]Aborted (Esc).[/yellow]")
        except KeyboardInterrupt:
            rt.kernel.interrupt()
            active_console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as exc:
            active_console.print(f"[red]Error: {escape(str(exc))}[/red]")


def _build_runtime(args: argparse.Namespace) -> tuple[_CLIRuntime, bool]:
    from rich.markup import escape

    from agent.commands import _CLIRuntime
    from agent.kernel_client import KernelClient
    from agent.loop import TurnUsage
    from agent.prompt import build_system_prompt
    from agent.session import Session
    from agent.ui import _short_path

    active_console = _sym("console")
    build_client_fn = _sym("_build_client")
    cwd = args.cwd.resolve()
    max_context_tokens, compact_reserve_tokens, keep_recent_tokens = _context_budget_from_env()

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
        client, provider_name, model, effort = build_client_fn(
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
            active_console.print(f"[red]Error: No previous session found for {escape(_short_path(cwd))}.[/red]")
            sys.exit(1)
        try:
            if args.fork:
                active_session, messages = Session.fork(target_id, cwd)
            else:
                active_session, messages = Session.load(target_id)
        except FileNotFoundError as exc:
            kernel.close()
            active_console.print(f"[red]Error: {escape(str(exc))}[/red]")
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
        keep_recent_tokens=keep_recent_tokens,
        verbose=args.verbose,
        plain_output=bool(args.prompt) or not sys.stdout.isatty(),
        headless=bool(args.prompt),
    )
    return rt, resumed


def _context_budget_from_env() -> tuple[int, int, int]:
    """Read (max_context_tokens, compact_reserve_tokens, keep_recent_tokens) from env."""
    def _env_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return (
        _env_int("MAX_CONTEXT_TOKENS", 128_000),
        _env_int("COMPACT_RESERVE_TOKENS", 16_384),
        _env_int("COMPACT_KEEP_RECENT_TOKENS", DEFAULT_KEEP_RECENT_TOKENS),
    )


def _load_env_files() -> None:
    """Load ~/.quartz/.env then the project .env, without overriding real env vars.

    Runs before any subcommand dispatch: `quartz auth`, `quartz mcp` and
    `quartz acp` all read credentials from the environment.
    """
    from dotenv import load_dotenv

    quartz_env = Path.home() / ".quartz" / ".env"
    if quartz_env.is_file():
        load_dotenv(quartz_env, override=False)
    load_dotenv(override=False)


def main() -> None:
    _load_env_files()

    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        _sym("_handle_mcp_cli")(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _sym("_handle_auth_cli")(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "acp":
        sys.argv[1] = "--acp"

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        pass

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
        choices=["auto", *PROVIDERS.keys()],
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

        max_context_tokens, compact_reserve_tokens, keep_recent_tokens = _context_budget_from_env()
        server = ACPServer(
            lambda: _sym("_build_client")(
                provider=args.provider, model_override=args.model, effort_override=args.effort
            ),
            max_context_tokens=max_context_tokens,
            compact_reserve_tokens=compact_reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
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
        rt.wait_snapshot()
        if rt.messages and not rt.headless and rt.kernel.dirty:
            try:
                rt.active_session.save_kernel(rt.kernel)
            except Exception:
                pass
        rt.kernel.close()
