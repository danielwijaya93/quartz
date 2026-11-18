"""CLI entrypoint for quartz (interactive REPL, `-p` headless mode, and `mcp` subcommands)."""

from __future__ import annotations

import argparse
import codecs
import dataclasses
import os
import queue
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import Application, PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text

from agent.actions import describe_cell_actions, describe_group_actions
from agent.compact import compact_messages
from agent.completer import AgentCompleter
from agent.google_auth import (
    auth_file_path,
    delete_google_credentials,
    load_google_credentials,
    login_google_interactive,
)
from agent.google_provider import GoogleAntigravityClient
from agent.images import (
    ImageAttachment,
    build_user_content,
    content_to_text,
    extract_inline_image_mentions,
    load_clipboard_image,
    load_image_file,
)
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnAborted, TurnUsage, agent_loop, format_bash_done_events
from agent.mcp_client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from agent.models import PROVIDERS, load_model_config, save_model_config
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


def render_markdown_block(target: Console, block: str) -> Text | None:
    """Render one Markdown block to Text with no leading or trailing blank lines."""
    options = target.options.update(width=target.width)
    rendered: list[Text] = []
    for segments in target.render_lines(Markdown(block), options, pad=False):
        line = Text()
        for segment in segments:
            if segment.control:
                continue
            line.append(segment.text, style=segment.style)
        rendered.append(line)

    while rendered and not rendered[0].plain.strip():
        rendered.pop(0)
    while rendered and not rendered[-1].plain.strip():
        rendered.pop()
    if not rendered:
        return None
    return Text("\n").join(rendered)


class _MidTurnInputWatcher:
    """Listens for Esc (abort) and typed steering lines while a turn is executing."""

    def __init__(
        self,
        abort_event: threading.Event,
        steering_queue: queue.Queue[str],
        *,
        on_abort: Callable[[], None] | None = None,
        on_queued: Callable[[str], None] | None = None,
    ) -> None:
        self._abort_event = abort_event
        self._steering_queue = steering_queue
        self._on_abort = on_abort
        self._on_queued = on_queued
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_attrs: list[Any] | None = None

    def __enter__(self) -> _MidTurnInputWatcher:
        if not sys.stdin.isatty():
            return self
        try:
            self._fd = sys.stdin.fileno()
            self._old_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._fd = None
            self._old_attrs = None
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
        if self._fd is not None and self._old_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except Exception:
                pass

    def _run(self) -> None:
        assert self._fd is not None
        buf: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except Exception:
                return
            if not ready:
                continue
            try:
                raw = os.read(self._fd, 1)
            except OSError:
                return
            if not raw:
                return
            text = decoder.decode(raw)
            for ch in text:
                if ch == "\x1b":
                    # Distinguish standalone Esc from terminal escape sequences (arrows, etc.)
                    more, _, _ = select.select([self._fd], [], [], 0.05)
                    if not more:
                        self._abort_event.set()
                        if self._on_abort:
                            self._on_abort()
                        return
                    # Drain rest of escape sequence
                    while select.select([self._fd], [], [], 0.005)[0]:
                        os.read(self._fd, 16)
                    continue
                if ch in ("\r", "\n"):
                    msg = "".join(buf).strip()
                    buf.clear()
                    if msg:
                        self._steering_queue.put(msg)
                        if self._on_queued:
                            self._on_queued(msg)
                    continue
                if ch in ("\x7f", "\x08"):
                    if buf:
                        buf.pop()
                    continue
                if ch >= " ":
                    buf.append(ch)


class BlockPrinter:
    """Print Markdown blocks, cells, and notices separated by exactly one blank line."""

    def __init__(self, target: Console) -> None:
        self._console = target
        self._started = False

    def reset(self) -> None:
        """Mark the start of a turn so the first block gets no leading blank line."""
        self._started = False

    def _separate(self) -> None:
        if self._started:
            self._console.print()
        self._started = True

    def markdown(self, block: str) -> None:
        text = render_markdown_block(self._console, block)
        if text is None:
            return
        self._separate()
        self._console.print(text, soft_wrap=True)

    def block(self, renderable: Any) -> None:
        self._separate()
        self._console.print(renderable)


printer = BlockPrinter(console)


def _render_tool_call(code: str) -> None:
    syntax = Syntax(code.rstrip(), "python", theme="monokai", line_numbers=False)
    printer.block(Panel(syntax, title="[bold cyan]ipython[/bold cyan]", border_style="cyan"))


def _render_tool_result(res: CellResult) -> None:
    text = res.format_for_model()
    style = "green" if res.ok else "red"
    title = "[bold green]out[/bold green]" if res.ok else "[bold red]error[/bold red]"
    printer.block(Panel(Text(text), title=title, border_style=style))


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

    action = describe_cell_actions(code)
    if res.ok:
        out_summary = (
            f"{len(out_lines)} lines out"
            if len(out_lines) > 1
            else (out_lines[0][:50] if out_lines else "no output")
        )
        return f"✔ [#{index}] {action} ({len(code_lines)}L: {code_preview}) → {out_summary} ({duration:.2f}s)"
    err_lines = [ln.strip() for ln in (res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:70] if err_lines else "error"
    return f"✖ [#{index}] {action} ({len(code_lines)}L: {code_preview}) → {err_preview} ({duration:.2f}s)"


def _render_compaction(summarized: int, kept: int) -> None:
    printer.block(
        Text(
            f"[compact] Summarized {summarized} older messages; kept {kept} recent messages and Python kernel state.",
            style="yellow",
        )
    )


def _render_bash_notice(notice: str) -> None:
    printer.block(Panel(Text(notice), title="[bold magenta]harness notice[/bold magenta]", border_style="magenta"))


def format_grouped_cell_summary(
    entries: list[tuple[int, str, CellResult, float]],
) -> tuple[str, bool]:
    """Format a 1-line summary for 1 or more consecutive ipython cells.

    Returns (summary_text, all_ok).
    """
    if len(entries) == 1:
        idx, code, res, duration = entries[0]
        return format_compact_cell_summary(idx, code, res, duration), res.ok

    start_idx = entries[0][0]
    end_idx = entries[-1][0]
    total_dur = sum(d for _, _, _, d in entries)
    action = describe_group_actions([code for _, code, _, _ in entries])
    failed = [(idx, res) for idx, _, res, _ in entries if not res.ok]
    if not failed:
        return (
            f"✔ [#{start_idx}..#{end_idx}] {action} ({len(entries)} cells, {total_dur:.2f}s) — /expand {start_idx}..{end_idx}",
            True,
        )
    last_idx, last_res = failed[-1]
    out_text = last_res.format_for_model().strip()
    err_lines = [ln.strip() for ln in (last_res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:60] if err_lines else "error"
    return (
        f"✖ [#{start_idx}..#{end_idx}] {action} "
        f"({len(entries)} cells, {len(failed)} failed, last #{last_idx}: {err_preview}) ({total_dur:.2f}s) — /expand {last_idx}",
        False,
    )


class CellGroupBuffer:
    """Buffers consecutive ipython cell results and flushes them as one summary block."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str, CellResult, float]] = []

    def add(self, index: int, code: str, res: CellResult, duration: float) -> None:
        self.entries.append((index, code, res, duration))

    def flush(self) -> None:
        if not self.entries:
            return
        summary, all_ok = format_grouped_cell_summary(self.entries)
        self.entries.clear()
        printer.block(Text(summary, style="dim green" if all_ok else "red"))


def _parse_expand_range(arg: str, total: int) -> tuple[int, int]:
    """Parse `/expand` argument (`""`, `"N"`, or `"A..B"`) into inclusive 1-based `(start, end)`."""
    if not arg:
        return total, total
    if ".." in arg:
        left, right = arg.split("..", 1)
        start = int(left.strip())
        end = int(right.strip())
        if not (1 <= start <= end <= total):
            raise ValueError
        return start, end
    idx = int(arg)
    if not (1 <= idx <= total):
        raise ValueError
    return idx, idx


def _pick_option(
    title: str, options: list[tuple[str, str]], current: str | None = None
) -> str | None:
    """Generic inline arrow-key picker (Up/Down, Enter, Esc) returning the chosen value."""
    selected_idx = [0]
    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                selected_idx[0] = i
                break

    def _get_text() -> list[tuple[str, str]]:
        frags: list[tuple[str, str]] = [
            ("bold", f"{title} (↑/↓ to move, Enter to select, Esc to cancel):\n")
        ]
        for i, (_, label) in enumerate(options):
            is_sel = i == selected_idx[0]
            marker = "❯ " if is_sel else "  "
            style = "reverse bold cyan" if is_sel else ""
            frags.append((style, f"{marker}{label}\n"))
        return frags

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event: Any) -> None:
        selected_idx[0] = (selected_idx[0] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _(event: Any) -> None:
        selected_idx[0] = (selected_idx[0] + 1) % len(options)

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=options[selected_idx[0]][0])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event: Any) -> None:
        event.app.exit(result=None)

    app: Application[str | None] = Application(
        layout=Layout(Window(content=FormattedTextControl(_get_text), always_hide_cursor=True)),
        key_bindings=kb,
        full_screen=False,
    )
    return app.run()


def _replay_session(
    messages: list[dict[str, Any]],
    cell_history: list[tuple[str, CellResult, float]],
    *,
    verbose: bool = False,
) -> None:
    """Replay a resumed session's full conversation and tool cells into the terminal."""
    import json

    printer.reset()
    cell_group = CellGroupBuffer()
    tool_outputs: dict[str, str] = {
        str(m["tool_call_id"]): str(m.get("content", ""))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            cell_group.flush()
            text = content_to_text(content)
            if text.startswith("[") and text.endswith("]") or text.startswith("[bash-done") or text.startswith("[compaction-summary]") or text.startswith("[python-state"):
                _render_bash_notice(text)
            else:
                console.print(Text("\n> " + text, style="bold"))
        elif role == "assistant":
            if content:
                cell_group.flush()
                printer.markdown(str(content))
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
                    cell_group.add(len(cell_history), code, cell_res, 0.0)
    cell_group.flush()


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{max(0.0, seconds):.1f}s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    mins, secs = divmod(whole, 60)
    return f"{mins}m {secs}s"


def _format_turn_timing(turn_seconds: float, total_seconds: float, turn_count: int) -> str:
    unit = "turn" if turn_count == 1 else "turns"
    return f"Turn took {_fmt_duration(turn_seconds)} (Total time {_fmt_duration(total_seconds)} · {turn_count} {unit})"


def _format_turn_footer(usage: TurnUsage, max_context_tokens: int) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    return f"(ctx: {_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%) | cache: {cache_pct:.0f}%)"


def _short_path(path: str | Path) -> str:
    """Render a path with the home directory collapsed to `~`."""
    text = str(path)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def _format_toolbar(
    model: str,
    effort: str | None,
    workspace: str | Path,
    usage: TurnUsage,
    max_context_tokens: int,
    bg_count: int,
) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    model_label = f"{model} ({effort})" if effort else model
    parts = [
        f" {model_label}",
        _short_path(workspace),
        f"ctx:{_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%)",
        f"cache:{cache_pct:.0f}%",
    ]
    if bg_count > 0:
        parts.append(f"bg:{bg_count}")
    return " | ".join(parts) + " "


def _build_client(
    provider: str = "auto",
    model_override: str | None = None,
    effort_override: str | None = None,
) -> tuple[Any, str, str, str]:
    api_key = os.getenv("OPENAI_API_KEY")
    has_google = load_google_credentials() is not None
    saved = load_model_config()
    env_model = model_override or os.getenv("MODEL")

    if provider == "auto":
        saved_provider = saved.get("provider")
        if saved_provider in PROVIDERS:
            chosen_provider = saved_provider
        elif env_model and env_model.startswith("gemini-") and has_google:
            chosen_provider = "google"
        elif not api_key and has_google:
            chosen_provider = "google"
        else:
            chosen_provider = "deepseek"
    elif provider == "openai":
        chosen_provider = "deepseek"
    else:
        chosen_provider = provider

    spec = PROVIDERS[chosen_provider]
    if model_override:
        model = model_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("model"):
        model = saved["model"]
    elif env_model and env_model in spec["models"]:
        model = env_model
    else:
        model = spec["default_model"]

    if effort_override:
        effort = effort_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("effort"):
        effort = saved["effort"]
    else:
        effort = os.getenv("EFFORT") or spec["default_effort"]

    if spec["backend"] == "google":
        if not has_google:
            console.print("[red]Error: Not logged in to Google. Run `quartz auth login google` first.[/red]")
            sys.exit(1)
        return GoogleAntigravityClient(), chosen_provider, model, effort

    if not api_key:
        console.print(
            "[red]Error: No credentials found. Either add OPENAI_API_KEY to ~/.quartz/.env or run `quartz auth login google`.[/red]"
        )
        sys.exit(1)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, chosen_provider, model, effort


def _handle_auth_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz auth",
        description="Manage subscription logins in ~/.quartz/auth.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    login_p = sub.add_parser("login", help="Log in with a subscription provider")
    login_p.add_argument("provider", choices=["google"], help="Provider to authenticate")

    sub.add_parser("status", help="Show current authentication status")

    logout_p = sub.add_parser("logout", help="Remove stored credentials")
    logout_p.add_argument("provider", choices=["google"], help="Provider to log out")

    args = parser.parse_args(argv)

    if args.action == "login":
        try:
            creds = login_google_interactive()
        except Exception as exc:
            console.print(f"[red]Login failed: {exc}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Logged in to Google![/green] Project: [bold]{creds.project_id}[/bold] "
            f"(saved to {auth_file_path()})"
        )
        return

    if args.action == "status":
        creds = load_google_credentials()
        if creds is None:
            console.print(f"[dim]Google: not logged in ({auth_file_path()})[/dim]")
        else:
            status = "expired (will auto-refresh)" if creds.is_expired() else "active"
            console.print(
                f"[green]Google: logged in[/green] | project: [bold]{creds.project_id}[/bold] | token: {status}"
            )
        return

    if args.action == "logout":
        if delete_google_credentials():
            console.print("[green]Logged out from Google.[/green]")
        else:
            console.print("[dim]No Google credentials found.[/dim]")


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
            console.print(f"  [cyan]{escape(name)}[/cyan] [{status}] — {escape(target)}{extra_str}")
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


@dataclasses.dataclass
class _CLIRuntime:
    """Mutable session and UI state for one CLI invocation."""

    cwd: Path
    kernel: KernelClient
    client: Any
    provider_name: str
    model: str
    effort: str
    system_prompt: str
    active_session: Session
    messages: list[dict[str, Any]]
    usage_stats: TurnUsage
    max_context_tokens: int
    compact_reserve_tokens: int
    keep_recent_messages: int
    verbose: bool
    plain_output: bool
    headless: bool
    cell_history: list[tuple[str, CellResult, float]] = dataclasses.field(default_factory=list)
    pending_images: list[ImageAttachment] = dataclasses.field(default_factory=list)
    total_elapsed_s: float = 0.0
    turn_count: int = 0

    def persist(self, *, compacted: bool = False) -> None:
        self.active_session.save_messages(self.messages, compacted=compacted)
        if self.usage_stats.prompt_tokens > 0 or self.usage_stats.total_prompt_tokens > 0:
            self.active_session.save_usage(self.usage_stats.to_dict())
        if not self.headless:
            self.active_session.save_kernel(self.kernel)


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
    streamed_any = [False]
    md_streamer = MarkdownBlockStreamer(printer.markdown)
    cell_group = CellGroupBuffer()

    def _on_status(label: str | None) -> None:
        if rt.plain_output:
            return
        if label is None:
            if active_status:
                active_status.pop().__exit__(None, None, None)
            return
        if label == "Running ipython...":
            action = describe_cell_actions(pending_cell_code[-1]) if pending_cell_code else "Run Python"
            if cell_group.entries:
                label = f"{action} #{len(rt.cell_history) + 1} ({len(cell_group.entries)} done)..."
            else:
                label = f"{action}..."
        if active_status:
            active_status[-1].update(label)
        else:
            st = console.status(label, spinner="dots")
            st.__enter__()
            active_status.append(st)

    def _on_text_delta(chunk: str) -> None:
        cell_group.flush()
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
            on_compaction=lambda s, k: (cell_group.flush(), _render_compaction(s, k)),
            on_bash_notice=lambda n: (cell_group.flush(), _render_bash_notice(n)),
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
        cell_group.flush()
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
    printer.block(Text(_format_turn_timing(turn_elapsed, rt.total_elapsed_s, rt.turn_count), style="dim"))
    if tokens:
        printer.block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))


def _handle_slash_command(rt: _CLIRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command."""
    if user_input in ("/exit", "/quit"):
        return True
    if user_input == "/help":
        console.print(
            Text(
                "/model           Choose provider, model, and reasoning effort\n"
                "/continue [id]   Pick and resume a previous session in this directory\n"
                "/image [path]    Attach an image for the next message (or: clear)\n"
                "/paste           Attach an image from the system clipboard\n"
                "/vars            List live Python variables in the kernel\n"
                "/session         Show active session ID and file paths\n"
                "/usage           Show context window usage and cache hit rate\n"
                "/expand [n|a..b] Expand a collapsed ipython cell or range (default: latest)\n"
                "/compact [focus] Summarize older messages while keeping the kernel alive\n"
                "/prompt          Show the assembled system prompt\n"
                "/reset           Clear conversation history, start a new session, and restart the kernel\n"
                "/exit            Exit"
            )
        )
        return False
    if user_input == "/paste":
        try:
            img = load_clipboard_image()
        except Exception as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            return False
        rt.pending_images.append(img)
        console.print(
            f"[green]Attached clipboard image ([bold]{escape(img.mime_type)}[/bold]); "
            f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
        )
        return False
    if user_input == "/image" or user_input.startswith("/image "):
        arg = user_input[len("/image") :].strip()
        if not arg:
            if not rt.pending_images:
                console.print("[dim]No images queued. Use /image <path>, /paste, or @image_path.[/dim]")
            else:
                names = ", ".join(img.label for img in rt.pending_images)
                console.print(f"[cyan]Queued ({len(rt.pending_images)}):[/cyan] {escape(names)}")
            return False
        if arg == "clear":
            rt.pending_images.clear()
            console.print("[dim]Cleared queued images.[/dim]")
            return False
        img_path = Path(arg).expanduser()
        if not img_path.is_absolute():
            img_path = rt.cwd / img_path
        try:
            img = load_image_file(img_path)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            return False
        rt.pending_images.append(img)
        console.print(
            f"[green]Attached [bold]{escape(img.label)}[/bold] ([cyan]{escape(img.mime_type)}[/cyan]); "
            f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
        )
        return False
    if user_input == "/model":
        provider_opts: list[tuple[str, str]] = []
        for pname, pspec in PROVIDERS.items():
            if pspec["backend"] == "google":
                ready = "logged in" if load_google_credentials() is not None else "not logged in"
            else:
                ready = "configured" if os.getenv("OPENAI_API_KEY") else "no API key"
            provider_opts.append((pname, f"{pspec['label']} ({ready})"))

        picked_provider = _pick_option("Provider", provider_opts, current=rt.provider_name)
        if not picked_provider:
            console.print("[dim]Cancelled.[/dim]")
            return False

        pspec = PROVIDERS[picked_provider]
        if len(pspec["models"]) > 1:
            model_opts = [(m, m) for m in pspec["models"]]
            picked_model = _pick_option("Model", model_opts, current=rt.model)
            if not picked_model:
                console.print("[dim]Cancelled.[/dim]")
                return False
        else:
            picked_model = pspec["models"][0]

        effort_opts = [(e, e) for e in pspec["efforts"]]
        picked_effort = _pick_option(
            f"Reasoning Effort ({picked_model})",
            effort_opts,
            current=rt.effort if picked_provider == rt.provider_name else pspec["default_effort"],
        )
        if not picked_effort:
            console.print("[dim]Cancelled.[/dim]")
            return False

        try:
            rt.client, rt.provider_name, rt.model, rt.effort = _build_client(
                provider=picked_provider,
                model_override=picked_model,
                effort_override=picked_effort,
            )
        except SystemExit:
            return False
        save_model_config(rt.provider_name, rt.model, rt.effort)
        console.print(
            f"[green]Switched to [bold]{rt.provider_name}[/bold] / [bold]{rt.model}[/bold] "
            f"(effort: [bold]{rt.effort}[/bold])[/green]"
        )
        return False
    if user_input.startswith("/continue"):
        arg_id = user_input[len("/continue") :].strip() or None
        if arg_id:
            chosen_id = arg_id
        else:
            sessions_list = Session.list_for_cwd(rt.cwd)
            if not sessions_list:
                console.print(f"[dim]No saved sessions found for {escape(_short_path(rt.cwd))}.[/dim]")
                return False
            session_opts = [
                (
                    item["id"],
                    f"{item['id']} | {item['updated_at']} | {item['message_count']} msgs"
                    f"{' (current)' if item['id'] == rt.active_session.id else ''}"
                    f"{' — ' + item['preview'] if item['preview'] else ''}",
                )
                for item in sessions_list
            ]
            chosen_id = _pick_option("Select a session", session_opts, current=rt.active_session.id)
            if not chosen_id:
                console.print("[dim]Cancelled.[/dim]")
                return False

        if chosen_id == rt.active_session.id:
            console.print(f"[dim]Already on session {rt.active_session.id}.[/dim]")
            return False

        try:
            new_session, new_messages = Session.load(chosen_id)
        except FileNotFoundError as exc:
            console.print(f"[red]Error: {escape(str(exc))}[/red]")
            return False

        rt.active_session = new_session
        rt.messages = new_messages
        rt.cell_history.clear()
        rt.total_elapsed_s = 0.0
        rt.turn_count = 0
        rt.kernel.restart()
        restored_notice = rt.active_session.restore_kernel(rt.kernel)
        if restored_notice:
            rt.messages.append({"role": "user", "content": restored_notice})
        rt.usage_stats = TurnUsage.from_dict(rt.active_session.last_usage)
        console.print(f"\n[bold cyan]Switched to session {rt.active_session.id}[/bold cyan]")
        _replay_session(rt.messages, rt.cell_history, verbose=rt.verbose)
        if rt.usage_stats.prompt_tokens > 0:
            printer.block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))
        return False
    if user_input.startswith("/expand") or user_input == "/last":
        if not rt.cell_history:
            console.print("[dim]No ipython cells executed in this session yet.[/dim]")
            return False
        arg = user_input[len("/expand") :].strip() if user_input.startswith("/expand") else ""
        try:
            start_idx, end_idx = _parse_expand_range(arg, len(rt.cell_history))
        except ValueError:
            console.print(f"[red]Invalid cell index or range. Choose 1..{len(rt.cell_history)}.[/red]")
            return False
        for idx in range(start_idx, end_idx + 1):
            code, res, duration = rt.cell_history[idx - 1]
            console.print(f"[dim]Cell #{idx} ({duration:.2f}s)[/dim]")
            _render_tool_call(code)
            _render_tool_result(res)
        return False
    if user_input == "/usage":
        pct = rt.usage_stats.context_pct(rt.max_context_tokens)
        cache_pct = rt.usage_stats.cache_hit_rate * 100.0
        total_cache = (
            (rt.usage_stats.total_cached_tokens / rt.usage_stats.total_prompt_tokens * 100.0)
            if rt.usage_stats.total_prompt_tokens > 0
            else 0.0
        )
        console.print(
            f"Last context:  {rt.usage_stats.prompt_tokens:,} / {rt.max_context_tokens:,} ({pct:.1f}%)\n"
            f"Last cache:    {rt.usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
            f"Session total: {rt.usage_stats.total_prompt_tokens:,} in / "
            f"{rt.usage_stats.total_completion_tokens:,} out / "
            f"{rt.usage_stats.total_cached_tokens:,} cached ({total_cache:.1f}%)"
        )
        return False
    if user_input == "/session":
        console.print(
            f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
            f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
            f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
        )
        return False
    if user_input == "/vars":
        vars_info = rt.kernel.list_vars()
        if not vars_info:
            console.print("[dim]No user-defined variables in the kernel.[/dim]")
        else:
            for name, typ in vars_info.items():
                line = Text("  ")
                line.append(name, style="cyan")
                line.append(f": {typ}")
                console.print(line)
        return False
    if user_input == "/prompt":
        console.print(Panel(Text(rt.system_prompt), title="System Prompt", border_style="dim"))
        return False
    if user_input == "/reset":
        rt.messages.clear()
        rt.cell_history.clear()
        rt.pending_images.clear()
        rt.usage_stats = TurnUsage()
        rt.total_elapsed_s = 0.0
        rt.turn_count = 0
        rt.kernel.restart()
        rt.active_session = Session.create(rt.cwd)
        console.print("[yellow]Conversation cleared, kernel restarted.[/yellow]")
        return False
    if user_input.startswith("/compact"):
        focus = user_input[len("/compact") :].strip() or None
        rt.messages, summarized, kept = compact_messages(
            rt.client, rt.model, rt.messages, rt.kernel, keep_recent=rt.keep_recent_messages, focus=focus
        )
        if summarized > 0:
            rt.persist(compacted=True)
            _render_compaction(summarized, kept)
        else:
            console.print("[dim]Nothing to compact yet.[/dim]")
        return False
    return None


def _run_interactive(rt: _CLIRuntime, resumed: bool) -> None:
    console.print(Text("quartz", style="bold blue"))

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
    prompt_session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_path)),
        completer=AgentCompleter(rt.cwd),
        complete_while_typing=True,
        key_bindings=kb,
        prompt_continuation="... ",
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
                    while not stop_watcher.is_set():
                        with rt.kernel._events_cond:
                            if not rt.kernel._events:
                                rt.kernel._events_cond.wait(timeout=0.25)
                            if rt.kernel._events:
                                app = prompt_session.app
                                if app.is_running:
                                    app.exit(result=_WAKEUP_SENTINEL)
                                return

                watcher_thread = threading.Thread(target=_watch_wakeup, daemon=True)
                watcher_thread.start()

            try:
                toolbar = _format_toolbar(
                    rt.model, rt.effort, rt.cwd, rt.usage_stats, rt.max_context_tokens, bg_count
                )
                user_input = prompt_session.prompt("\n> ", bottom_toolbar=toolbar).strip()
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
