"""CLI entrypoint for quartz (interactive REPL, `-p` headless mode, and `mcp` subcommands)."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
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
from quartz.kernel.client import KernelClient
from quartz.core.loop import TurnUsage, drain_bash_events, format_bash_done_events
from quartz.providers.registry import PROVIDERS, resolve_context_tokens
from quartz.core.progress import resolve_every
from quartz.core.prompt import build_system_prompt
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnStatus, run_interactive_turn, run_turn
from quartz.providers.selection import build_provider
from quartz.cli.subcommands import _handle_auth_cli, _handle_mcp_cli
from quartz.tui.render import (
    _active_printer,
    _format_turn_footer,
    _format_turn_timing,
    _render_bash_notice,
    _short_path,
    console,
)
from quartz.tui.replay import _replay_session
from quartz.tui.turn_view import make_turn_view


def _run_turn(rt: AgentRuntime) -> tuple[str, list[dict[str, Any]], int]:
    """Run one turn, rendered by the terminal view (`core.turn.run_turn` + `TuiTurnView`)."""
    return run_turn(rt, make_turn_view(rt))


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
            _active_printer().block(
                Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim")
            )

    def _run_one_turn() -> None:
        outcome = run_interactive_turn(rt, make_turn_view(rt))
        if outcome.status is TurnStatus.ABORTED:
            console.print("\n[yellow]Aborted (Esc).[/yellow]")
        elif outcome.status is TurnStatus.INTERRUPTED:
            console.print("\n[yellow]Interrupted.[/yellow]")
        elif outcome.status is TurnStatus.ERROR:
            console.print(f"[red]Error: {escape(str(outcome.error))}[/red]")
        else:
            timing = _format_turn_timing(outcome.elapsed_s, rt.total_elapsed_s, rt.turn_count)
            footer = (
                f"{timing} · {_format_turn_footer(rt.usage_stats, rt.max_context_tokens)}"
                if outcome.tokens
                else timing
            )
            _active_printer().block(Text(footer, style="dim"))

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
        progress_every=resolve_every(
            args.progress_every,
            headless=bool(args.prompt),
            default=_settings.progress_every,
        ),
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
        "--progress-every",
        type=int,
        metavar="N",
        default=None,
        help=(
            "Ask the model to update the user every N ipython calls (0 disables). "
            "Default: on for interactive sessions, off for -p."
        ),
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
            progress_every=_settings.progress_every,
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
