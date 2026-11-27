"""CLI entrypoint for quartz (interactive REPL, `-p` headless mode, and `mcp` subcommands)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from quartz.bootstrap import BootstrapOptions
    from quartz.core.runtime import AgentRuntime

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "BootstrapOptions": ("quartz.bootstrap", "BootstrapOptions"),
    "build_runtime": ("quartz.bootstrap", "build_runtime"),
    "_handle_auth_cli": ("quartz.cli.subcommands", "_handle_auth_cli"),
    "_handle_mcp_cli": ("quartz.cli.subcommands", "_handle_mcp_cli"),
    "PROVIDERS": ("quartz.providers.registry", "PROVIDERS"),
    "resolve_context_tokens": ("quartz.providers.registry", "resolve_context_tokens"),
    "build_provider": ("quartz.providers.selection", "build_provider"),
    "load_settings": ("quartz.settings", "load_settings"),
    "project_trust_status": ("quartz.settings", "project_trust_status"),
    "trust_project": ("quartz.settings", "trust_project"),
    "_active_printer": ("quartz.tui.render", "_active_printer"),
    "_format_turn_footer": ("quartz.tui.render", "_format_turn_footer"),
    "_format_turn_timing": ("quartz.tui.render", "_format_turn_timing"),
    "_render_bash_notice": ("quartz.tui.render", "_render_bash_notice"),
    "_short_path": ("quartz.tui.render", "_short_path"),
    "console": ("quartz.tui.render", "console"),
    "make_turn_view": ("quartz.tui.turn_view", "make_turn_view"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    value = getattr(importlib.import_module(mod_name), attr_name)
    globals()[name] = value
    return value


def _get_attr(name: str) -> Any:
    """Resolve an attribute respecting monkeypatches in ``globals()``."""
    if name in globals():
        return globals()[name]
    return __getattr__(name)


def _print_error(message: str) -> None:
    """Print an error message, avoiding rich/TUI imports in headless mode unless loaded."""
    if "console" in globals():
        from rich.markup import escape

        globals()["console"].print(f"[red]Error: {escape(message)}[/red]")
        return
    if sys.stdout.isatty():
        from rich.markup import escape

        _get_attr("console").print(f"[red]Error: {escape(message)}[/red]")
        return
    sys.stderr.write(f"Error: {message}\n")
    sys.stderr.flush()


class _HeadlessTurnView:
    """Zero-rich plain stdout view for `quartz -p` (headless non-verbose) runs."""

    def __init__(self, rt: AgentRuntime) -> None:
        self._rt = rt
        self.streamed_any: list[bool] = [False]
        self.pending_cells: dict[str, tuple[str, float]] = {}
        self.turn_reasoning: list[str] = []

    def on_status(self, label: str | None) -> None:
        return None

    def on_thinking_delta(self, text: str) -> None:
        self.turn_reasoning.append(text)

    def on_text_delta(self, text: str) -> None:
        self.streamed_any[0] = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        if self.streamed_any[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.streamed_any[0] = False
        self.pending_cells[tool_call_id] = (code, time.monotonic())

    def on_tool_result(self, tool_call_id: str, cell: Any) -> None:
        code, started = self.pending_cells.pop(tool_call_id, ("", time.monotonic()))
        duration = time.monotonic() - started
        self._rt.cell_history.append((code, cell, duration))

    def on_compaction(self, result: Any) -> None:
        return None

    def on_bash_notice(self, notice: str) -> None:
        sys.stdout.write(notice.rstrip() + "\n")
        sys.stdout.flush()

    def on_steering(self, text: str) -> None:
        return None

    def reset(self) -> None:
        return None

    def finish(self) -> None:
        if self.streamed_any[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def show_final(self, answer: str) -> None:
        sys.stdout.write(answer.rstrip() + "\n")
        sys.stdout.flush()


async def _run_turn(rt: AgentRuntime) -> tuple[str, list[dict[str, Any]], int]:
    """Run one turn, rendered by the terminal view (`core.turn.run_turn` + `TuiTurnView`)."""
    from quartz.core.turn import run_turn

    return await run_turn(rt, _get_attr("make_turn_view")(rt))


def _emit_bash_notice(rt: AgentRuntime, notice: str) -> None:
    """Render a background bash notice and append it to the conversation."""
    _get_attr("_render_bash_notice")(notice)
    rt.messages.append({"role": "user", "content": notice})


async def _run_headless_async(rt: AgentRuntime, prompt: str, image_args: list[str]) -> None:
    from quartz.core.images import (
        ImageAttachment,
        build_user_content,
        extract_inline_image_mentions,
        load_image_file,
    )
    from quartz.core.turn import run_turn

    cli_images: list[ImageAttachment] = []
    for raw_img in image_args:
        img_path = Path(raw_img).expanduser()
        if not img_path.is_absolute():
            img_path = rt.cwd / img_path
        try:
            cli_images.append(load_image_file(img_path))
        except (FileNotFoundError, ValueError) as exc:
            _print_error(str(exc))
            return
    try:
        cli_images.extend(extract_inline_image_mentions(prompt, rt.cwd))
    except (OSError, ValueError) as exc:
        _print_error(str(exc))
        return
    rt.messages.append({"role": "user", "content": build_user_content(prompt, cli_images)})
    if getattr(rt, "plain_output", True) and not getattr(rt, "verbose", False) and not getattr(rt, "show_details", False):
        _, rt.messages, _ = await run_turn(rt, _HeadlessTurnView(rt))  # type: ignore[arg-type]
    else:
        _, rt.messages, _ = await _run_turn(rt)
    rt.persist()


def _run_headless(rt: AgentRuntime, prompt: str, image_args: list[str]) -> None:
    asyncio.run(_run_headless_async(rt, prompt, image_args))
    # Nothing to wait for afterwards: a `-p` kernel runs `bash()` inline, so by the
    # time the turn returns, every command it started has already finished and its
    # output is in the transcript. The old `--background-wait` loop existed only to
    # service jobs that outlived their turn, which headless no longer creates.


def _run_interactive(rt: AgentRuntime, resumed: bool) -> None:
    if not rt.plain_output and sys.stdin.isatty() and sys.stdout.isatty():
        from quartz.tui.dock import InteractiveDock

        InteractiveDock(
            rt,
            resumed=resumed,
            show_thinking=getattr(rt, "show_thinking", False),
            show_details=getattr(rt, "show_details", False),
        ).run()
        return

    from rich.markup import escape
    from rich.text import Text

    from quartz.core.images import build_user_content, extract_inline_image_mentions
    from quartz.core.loop import drain_bash_events
    from quartz.core.turn import TurnStatus, run_interactive_turn

    console = _get_attr("console")
    _active_printer = _get_attr("_active_printer")
    _format_turn_footer = _get_attr("_format_turn_footer")
    _format_turn_timing = _get_attr("_format_turn_timing")
    _render_bash_notice = _get_attr("_render_bash_notice")
    make_turn_view = _get_attr("make_turn_view")

    set_perm = getattr(rt.kernel, "set_permission_handler", None)
    if callable(set_perm) and sys.stdin.isatty():
        def _plain_permission(req: Any) -> str:
            console.print(f"\n[yellow]Permission required:[/yellow] {escape(req.command_display)}")
            console.print("  [1] Allow once  [2] Allow for session  [3] Reject")
            ans = sys.stdin.readline().strip()
            if ans == "1":
                return "allow_once"
            if ans == "2":
                return "allow_session"
            return "reject"
        set_perm(_plain_permission)

    console.print(Text.assemble(("quartz", "bold"), (" v0.1.0 · Run /help for commands", "dim")))

    if resumed:
        from quartz.tui.replay import _replay_session

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

    async def _run_one_turn() -> None:
        outcome = await run_interactive_turn(rt, make_turn_view(rt))
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

    async def _plain_loop() -> None:
        from quartz.tui.commands import _handle_slash_command_async

        while True:
            while rt.kernel.has_pending_events():
                notice = drain_bash_events(rt.kernel, rt.messages)
                if notice:
                    _render_bash_notice(notice)
                    await _run_one_turn()

            raw_line = await asyncio.to_thread(sys.stdin.readline)
            if not raw_line:
                break
            user_input = raw_line.strip()
            if not user_input:
                continue

            cmd_res = await _handle_slash_command_async(rt, user_input)
            if cmd_res is True:
                break
            if cmd_res is False:
                continue

            try:
                inline_images = extract_inline_image_mentions(user_input, rt.cwd)
            except (OSError, ValueError) as exc:
                console.print(f"[red]Error: {escape(str(exc))}[/red]")
                continue
            attached_images = [*rt.pending_images, *inline_images]
            rt.pending_images.clear()
            rt.messages.append({"role": "user", "content": build_user_content(user_input, attached_images)})
            await _run_one_turn()

    asyncio.run(_plain_loop())


def _bootstrap_options(args: argparse.Namespace) -> BootstrapOptions:
    """Translate the parsed command line into transport-neutral bootstrap input.

    The only decisions made here are the ones that genuinely belong to a terminal:
    `plain_output` folds `--prompt` together with TTY detection, which a server has
    no use for.
    """
    BootstrapOptions = _get_attr("BootstrapOptions")
    return BootstrapOptions(
        cwd=args.cwd,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        headless=bool(args.prompt),
        resume_id=args.resume,
        fork=args.fork,
        cont=args.cont,
        verbose=args.verbose,
        plain_output=bool(args.prompt) or not sys.stdout.isatty(),
        show_thinking=args.show_thinking,
        show_details=args.show_details,
        progress_every=args.progress_every,
        background_check_every=args.background_check_every,
        trust_project=getattr(args, "trust_project", False),
        sandbox=getattr(args, "sandbox", None),
    )


def _maybe_prompt_project_trust(args: argparse.Namespace) -> None:
    """Prompt once in interactive TTY mode when an untrusted folder has `.quartz/` config."""
    if args.prompt or getattr(args, "trust_project", False):
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    cwd = args.cwd.expanduser().resolve()
    status = _get_attr("project_trust_status")(cwd)
    if not status["needsTrust"]:
        return

    from quartz.tui.modals import _pick_option

    found = ", ".join(status["files"])
    choice = _pick_option(
        f"Trust {_get_attr('_short_path')(cwd)}? Found {found}",
        [
            ("trust", "Yes, trust this folder (remember and load .quartz/ config)"),
            ("skip", "No, keep untrusted for now (skip .quartz/ config)"),
        ],
    )
    if choice == "trust":
        _get_attr("trust_project")(cwd)


def _build_runtime(args: argparse.Namespace) -> tuple[AgentRuntime, bool]:
    """Build the runtime for a terminal run, or exit 1 with the same red line as before.

    `build_runtime` raises instead of exiting so a server can survive a bad request;
    the terminal keeps its old contract by reporting and exiting here.
    """
    from quartz.errors import CredentialError, EndpointConfigError, SessionNotFoundError
    from quartz.kernel.sandbox import SandboxUnavailableError

    try:
        return _get_attr("build_runtime")(_bootstrap_options(args))
    except SessionNotFoundError as exc:
        _print_error(f"No previous session found for {_get_attr('_short_path')(exc.cwd)}.")
        sys.exit(1)
    except (CredentialError, EndpointConfigError, FileNotFoundError, SandboxUnavailableError) as exc:
        _print_error(str(exc))
        sys.exit(1)


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
        _get_attr("_handle_mcp_cli")(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _get_attr("_handle_auth_cli")(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "prune":
        from quartz.cli.subcommands import _handle_prune_cli

        _handle_prune_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        from quartz.cli.subcommands import _handle_web_cli

        _handle_web_cli(sys.argv[2:])
        return
    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGTERM, _handle_sigterm)

    from quartz import __version__

    providers = _get_attr("PROVIDERS")
    parser = argparse.ArgumentParser(description="quartz — minimal coding agent with a persistent Python REPL.")
    parser.add_argument("-V", "--version", action="version", version=f"quartz {__version__}")
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
    parser.add_argument(
        "-r",
        "--resume",
        metavar="ID_OR_NAME",
        help="Resume a specific session by ID or name (names are scoped to the current directory).",
    )
    parser.add_argument("--fork", action="store_true", help="Fork the resumed session into a new session ID.")
    parser.add_argument(
        "--background-check-every",
        type=int,
        metavar="SECONDS",
        default=None,
        help=(
            "Report on a running background job every N seconds (0 disables). "
            "Default: on in interactive sessions, off for -p."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["auto", *providers.keys()],
        default=os.getenv("PROVIDER", "auto"),
        help="LLM provider backend (default: auto).",
    )
    parser.add_argument("--model", help="Override MODEL from environment.")
    parser.add_argument("--effort", help="Reasoning effort level (e.g. low, medium, high, max, xhigh).")
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
    parser.add_argument(
        "--trust-project",
        action="store_true",
        help="Trust the project in --cwd (allows .quartz/helpers.py and .quartz/mcp.json) and persist to config.",
    )
    sb_group = parser.add_mutually_exclusive_group()
    sb_group.add_argument(
        "--sandbox",
        dest="sandbox",
        action="store_true",
        default=None,
        help="Run the kernel inside the OS sandbox (default).",
    )
    sb_group.add_argument(
        "--sandbox-off",
        dest="sandbox",
        action="store_false",
        help="Disable the OS sandbox for this session.",
    )
    args = parser.parse_args()

    if args.fork and not args.resume and not args.cont:
        parser.error("--fork requires -r <session-id> (or -c)")

    from quartz.logging_config import configure_logging

    configure_logging()
    _maybe_prompt_project_trust(args)
    rt, resumed = _build_runtime(args)
    try:
        if args.prompt:
            _run_headless(rt, args.prompt, args.image)
        else:
            _run_interactive(rt, resumed)
    finally:
        rt.wait_snapshot()
        if rt.messages and not rt.headless and rt.kernel.dirty:
            try:
                rt.active_session.save_kernel(rt.kernel)
            except Exception:
                logger.debug("Kernel snapshot save on exit failed", exc_info=True)
        rt.kernel.close()
        aclose_client = getattr(rt.client, "aclose", None)
        if callable(aclose_client):
            try:
                res = aclose_client()
                if asyncio.iscoroutine(res):
                    asyncio.run(res)
            except Exception:
                logger.debug("Client close on exit failed", exc_info=True)
        try:
            from quartz.providers.http_pool import close_shared_async_http_clients

            asyncio.run(close_shared_async_http_clients())
        except Exception:
            logger.debug("HTTP pool close on exit failed", exc_info=True)
