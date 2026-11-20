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

from rich.markup import escape
from rich.text import Text

from quartz.tui.commands import _handle_slash_command
from quartz.errors import CredentialError, EndpointConfigError
from quartz.settings import load_settings
from quartz.core.images import (
    ImageAttachment,
    build_user_content,
    extract_inline_image_mentions,
    load_image_file,
)
from quartz.tui.dock import InteractiveDock
from quartz.kernel.client import CellResult, KernelClient
from quartz.core.loop import (
    TurnAborted,
    TurnUsage,
    agent_loop,
    drain_bash_events,
    format_bash_done_events,
)
from quartz.providers.registry import PROVIDERS, resolve_context_tokens
from quartz.core.prompt import build_system_prompt
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.cli.subcommands import _handle_auth_cli, _handle_mcp_cli, build_provider
from quartz.tui.render import (
    MarkdownBlockStreamer,
    TurnProgressTracker,
    _format_turn_footer,
    _format_turn_timing,
    _render_bash_notice,
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
    _short_path,
    console,
    printer,
)
from quartz.tui.replay import _replay_session


def _run_turn(
    rt: AgentRuntime,
    *,
    tracker: TurnProgressTracker | None = None,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    on_steering: Callable[[str], None] | None = None,
    on_thinking_delta: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    pending_cells: dict[str, tuple[str, float]] = {}
    streamed_any = [False]
    md_streamer = MarkdownBlockStreamer(printer.markdown)
    own_tracker = tracker is None
    if tracker is None:
        tracker = TurnProgressTracker(
            console,
            printer,
            verbose=rt.verbose,
            plain_output=rt.plain_output,
            show_thinking=getattr(rt, "show_thinking", False),
            show_details=getattr(rt, "show_details", False),
        )
    turn_stop = threading.Event()

    def _tick_tracker() -> None:
        while not turn_stop.wait(0.1):
            tracker.tick()

    if not rt.plain_output and not rt.verbose and getattr(tracker, "_live_renderer", None) is None:
        threading.Thread(target=_tick_tracker, daemon=True).start()

    def _on_status(label: str | None) -> None:
        if rt.plain_output:
            return
        if label == "Thinking...":
            tracker.start_thinking()
        elif label is None:
            tracker.stop_thinking()

    # Collector: reasoning text is persisted in assistant_dict (see
    # agent_loop); forwarding to the tracker drives the Ctrl+T trace.
    turn_reasoning: list[str] = []

    def _on_thinking_delta(chunk: str) -> None:
        turn_reasoning.append(chunk)
        tracker.add_thinking_delta(chunk)
        if on_thinking_delta is not None:
            on_thinking_delta(chunk)

    def _on_text_delta(chunk: str) -> None:
        tracker.flush()
        streamed_any[0] = True
        if rt.plain_output:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        else:
            md_streamer.feed(chunk)

    def _on_tool_call(tc_id: str, code: str) -> None:
        if streamed_any[0]:
            if rt.plain_output:
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                md_streamer.flush()
            streamed_any[0] = False
        pending_cells[tc_id] = (code, time.monotonic())
        if rt.verbose or getattr(tracker, "show_details", False) or getattr(rt, "show_details", False):
            tracker.flush()
            _render_tool_call(code)
        else:
            tracker.start_tool(code)

    def _on_tool_result(tc_id: str, res: CellResult) -> None:
        code, started = pending_cells.pop(tc_id, ("", time.monotonic()))
        duration = time.monotonic() - started
        rt.cell_history.append((code, res, duration))
        if rt.verbose or getattr(tracker, "show_details", False) or getattr(rt, "show_details", False):
            _render_tool_result(res)
        else:
            tracker.add_tool_result(len(rt.cell_history), code, res, duration)

    streamed_any[0] = False
    if own_tracker:
        printer.reset()
    cell_start_idx = len(rt.cell_history)
    try:
        answer, updated_msgs, tokens = agent_loop(
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
            on_thinking_delta=_on_thinking_delta,
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
    if hasattr(rt.active_session, "record_cell_meta"):
        new_cells = rt.cell_history[cell_start_idx:]
        if new_cells:
            unrecorded_ids: list[str] = []
            existing_meta = getattr(rt.active_session, "cell_meta", {})
            for m in updated_msgs:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        if tc.get("function", {}).get("name") == "ipython":
                            tc_id = str(tc.get("id", ""))
                            if tc_id and tc_id not in existing_meta:
                                unrecorded_ids.append(tc_id)
            for tc_id, (_, res, dur) in zip(unrecorded_ids[-len(new_cells):], new_cells):
                rt.active_session.record_cell_meta(tc_id, ok=res.ok, duration_s=dur)
    if not streamed_any[0] and answer:
        if rt.plain_output:
            sys.stdout.write(answer.rstrip() + "\n")
            sys.stdout.flush()
        else:
            printer.markdown(answer)
    return answer, updated_msgs, tokens


def _emit_bash_notice(rt: AgentRuntime, notice: str) -> None:
    """Render a background bash notice and append it to the conversation."""
    _render_bash_notice(notice)
    rt.messages.append({"role": "user", "content": notice})


def _run_headless(
    rt: AgentRuntime, prompt: str, image_args: list[str], background_wait: float
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
        _emit_bash_notice(rt, notice)
        _, rt.messages, _ = _run_turn(rt)
        rt.persist()


def _run_interactive(rt: AgentRuntime, resumed: bool) -> None:
    if not rt.plain_output and sys.stdin.isatty() and sys.stdout.isatty():
        InteractiveDock(
            rt,
            resumed=resumed,
            show_thinking=getattr(rt, "show_thinking", False),
            show_details=getattr(rt, "show_details", False),
        ).run()
        return

    console.print(Text.assemble(("quartz", "bold"), (" v0.1.0 · Run /help for commands", "dim")))

    if resumed:
        _replay_session(
            rt.messages,
            rt.cell_history,
            verbose=rt.verbose,
            cell_meta=getattr(rt.active_session, "cell_meta", None),
            steering_indices=getattr(rt.active_session, "steering_indices", None),
            show_thinking=getattr(rt, "show_thinking", False),
            show_details=getattr(rt, "show_details", False),
        )
        if rt.usage_stats.prompt_tokens > 0:
            printer.block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))

    def _run_one_turn() -> None:
        t0 = time.monotonic()
        try:
            _, rt.messages, tokens = _run_turn(rt)
        except TurnAborted:
            rt.kernel.interrupt()
            rt.persist()
            console.print("\n[yellow]Aborted (Esc).[/yellow]")
        except KeyboardInterrupt:
            rt.kernel.interrupt()
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
        else:
            turn_elapsed = time.monotonic() - t0
            rt.total_elapsed_s += turn_elapsed
            rt.turn_count += 1
            rt.persist()
            timing = _format_turn_timing(turn_elapsed, rt.total_elapsed_s, rt.turn_count)
            footer = f"{timing} · {_format_turn_footer(rt.usage_stats, rt.max_context_tokens)}" if tokens else timing
            printer.block(Text(footer, style="dim"))

    while True:
        while rt.kernel.has_pending_events():
            notice = drain_bash_events(rt.kernel, rt.messages)
            if notice:
                _render_bash_notice(notice)
                _run_one_turn()

        raw_line = sys.stdin.readline()
        if not raw_line:
            break
        user_input = raw_line.strip()
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
        _run_one_turn()


def _build_runtime(args: argparse.Namespace) -> tuple[AgentRuntime, bool]:
    cwd = args.cwd.resolve()

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
        client, provider_name, model, effort = build_provider(
            provider=args.provider, model_override=args.model, effort_override=args.effort
        )
        max_context_tokens = resolve_context_tokens(provider_name, model, client)
        _settings = load_settings()
        compact_reserve_tokens = _settings.compact_reserve_tokens
        keep_recent_tokens = _settings.keep_recent_tokens
        system_prompt = build_system_prompt(cwd, headless=bool(args.prompt))
    except (CredentialError, EndpointConfigError) as exc:
        kernel_thread.join()
        kernel.close()
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        sys.exit(1)
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

    rt = AgentRuntime(
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
        show_thinking=args.show_thinking,
        show_details=args.show_details,
    )
    return rt, resumed


def _context_budget_from_settings() -> tuple[int, int, int]:
    """Return (0, reserve, keep) from ~/.quartz/config.json via /settings.

    Kept for the ACP call site. Legacy COMPACT_* env vars are silently ignored.
    """
    settings = load_settings()
    return (0, settings.compact_reserve_tokens, settings.keep_recent_tokens)


def _load_env_files() -> None:
    """Load ~/.quartz/.env then the project .env, without overriding real env vars.

    Loads non-secret configuration only (MODEL, QUARTZ_*). Compaction
    preferences live in ~/.quartz/config.json (see /settings), not .env.
    API-key credentials are never read from .env: they are login-only
    (auth.json, via /login). Legacy keys such as OPENAI_API_KEY or
    OPENAI_BASE_URL may still be loaded into the process environment by
    dotenv, but they are silently ignored and never used.
    Runs before any subcommand dispatch.
    """
    from dotenv import load_dotenv

    quartz_env = Path.home() / ".quartz" / ".env"
    if quartz_env.is_file():
        load_dotenv(quartz_env, override=False)
    load_dotenv(override=False)


def main() -> None:
    _load_env_files()

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
    parser.add_argument(
        "--show-thinking",
        action="store_true",
        help="Show reasoning trace (Ctrl+T).",
    )
    parser.add_argument(
        "--show-details",
        action="store_true",
        help="Show full tool call details (Ctrl+O).",
    )
    args = parser.parse_args()

    if args.fork and not args.resume and not args.cont:
        parser.error("--fork requires -r <session-id> (or -c)")

    if args.acp:
        from quartz.cli.acp import ACPServer

        _settings = load_settings()
        compact_reserve_tokens = _settings.compact_reserve_tokens
        keep_recent_tokens = _settings.keep_recent_tokens

        def _provider_with_resolved_context():
            client, pname, model_name, effort_name = build_provider(
                provider=args.provider, model_override=args.model, effort_override=args.effort
            )
            resolved = resolve_context_tokens(pname, model_name, client)
            return client, pname, model_name, effort_name, resolved

        server = ACPServer(
            _provider_with_resolved_context,
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
