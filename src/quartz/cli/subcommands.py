"""The `mcp`, `auth` and `web` subcommands: argument parsing, and nothing else.

All three dispatch before the main parser runs (see `cli/app.py::main`). `auth` only
parses its flags here — the flows it runs are interactive and live in
`quartz.tui.provider_setup`. `web` builds the same runtime the terminal does, then
hands it to the browser transport; every import that needs FastAPI or uvicorn is
lazy so a user who never runs `quartz web` never loads them.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape

from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from quartz.providers.registry import PROVIDERS
from quartz.tui.provider_setup import _login_provider, _logout_provider, show_auth_status
from quartz.tui.render import console

__all__ = ["_handle_auth_cli", "_handle_mcp_cli", "_handle_prune_cli", "_handle_web_cli"]


def _handle_auth_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz auth",
        description="Manage provider logins (OAuth and API keys) in ~/.quartz/auth.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    provider_choices = list(PROVIDERS.keys())
    login_p = sub.add_parser("login", help="Log in with an OAuth or API-key provider")
    login_p.add_argument("provider", nargs="?", choices=provider_choices, help="Provider to authenticate")

    sub.add_parser("status", help="Show current authentication status")

    logout_p = sub.add_parser("logout", help="Remove stored credentials")
    logout_p.add_argument("provider", nargs="?", choices=provider_choices, help="Provider to log out")

    args = parser.parse_args(argv)

    if args.action == "login":
        if not _login_provider(args.provider):
            sys.exit(1)
        return

    if args.action == "status":
        show_auth_status()
        return

    if args.action == "logout":
        _logout_provider(args.provider)



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
            console.print("[red]Error: specify server name(s) or --all[/red]")
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


# -- web ---------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}


def _is_loopback(host: str) -> bool:
    """Whether binding ``host`` stays on this machine."""
    return host.strip().lower() in _LOOPBACK or host.strip().startswith("127.")


_PORT_SUGGEST_SPAN = 20
"""How many ports past the requested one to probe before giving up on a suggestion."""


def _can_bind(host: str, port: int) -> bool:
    """Whether a TCP listener could take ``host:port`` right now.

    Two probes, strict first. The plain bind is what catches a held port on every
    platform -- ``SO_REUSEADDR`` can let that bind through on some of them, which
    would turn a conflict into a false all-clear. The second probe then clears a
    port that is merely lingering in ``TIME_WAIT`` after a restart: uvicorn sets
    the option itself in its own ``bind_socket``, so it would bind there and
    refusing would be a false alarm.
    """
    if _bind_once(host, port, reuse=False):
        return True
    return _bind_once(host, port, reuse=True)


def _bind_once(host: str, port: int, *, reuse: bool) -> bool:
    """Bind, then close at once: the probe never lingers as a listener."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if reuse:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class _PortOwner:
    """The process holding a port, as far as this machine lets us look."""

    pid: int
    command: str


def _port_owner(port: int) -> _PortOwner | None:
    """The process listening on ``port``, or ``None`` when it cannot be known.

    Best effort and Linux-only: `ss` first, then `/proc` for the boxes where it is
    not installed. Nothing here may raise -- this runs while Quartz is already
    explaining a failure, and a traceback from the diagnostics would bury it.
    """
    try:
        found = _owner_via_ss(port) or _owner_via_proc(port)
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
        return None
    if found is None:
        return None
    pid, name = found
    return _PortOwner(pid=pid, command=_command_line(pid) or name)


def _owner_via_ss(port: int) -> tuple[int, str] | None:
    """Read the owner out of ``ss -tlnp`` (``users:(("python3",pid=1,fd=3))``)."""
    try:
        done = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        match = re.search(r'\("(?P<name>[^"]*)",pid=(?P<pid>\d+)', line)
        if match is None:
            continue
        # The local column is "address:port"; the peer column is never this port.
        local = line.split()[3] if len(line.split()) > 3 else ""
        if local.rpartition(":")[2] == str(port):
            return int(match.group("pid")), match.group("name")
    return None


def _owner_via_proc(port: int) -> tuple[int, str] | None:
    """Read the owner out of ``/proc/net/tcp*`` and the socket's owning ``/proc/<pid>``."""
    inode = _listening_inode(port)
    if inode is None:
        return None
    target = f"socket:[{inode}]"
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fds = os.scandir(f"/proc/{entry.name}/fd")
        except OSError:
            continue  # another user's process, or one that just exited
        with fds:
            for fd in fds:
                try:
                    if os.readlink(fd.path) == target:
                        return int(entry.name), ""
                except OSError:
                    continue
    return None


def _listening_inode(port: int) -> str | None:
    """The socket inode listening on ``port``, or ``None``; needs no privileges."""
    wanted = f"{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A is TCP_LISTEN
                continue
            if fields[1].rpartition(":")[2] == wanted:
                return fields[9]
    return None


def _command_line(pid: int) -> str:
    """A process's command line, or ``""`` when it is unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0") if part)


def _suggest_free_port(host: str, port: int, span: int = _PORT_SUGGEST_SPAN) -> int | None:
    """The first port after ``port`` that binds, or ``None`` when the range is full."""
    for candidate in range(port + 1, min(port + span, 65535) + 1):
        if _can_bind(host, candidate):
            return candidate
    return None


def _report_port_in_use(host: str, port: int) -> None:
    """Explain a taken port: what holds it, and the command that would work.

    One message path for both the pre-flight check and the `uvicorn.run` backstop,
    so the two can never drift apart. Never raises.
    """
    console.print(
        f"[red]Error: {escape(host)}:{port} is already in use, so the web client cannot start.[/red]"
    )
    owner = _port_owner(port)
    if owner is None:
        console.print("  Held by: another process on this machine (could not be identified).")
    elif owner.command:
        console.print(f"  Held by: pid {owner.pid} [cyan]{escape(owner.command)}[/cyan]")
    else:
        console.print(f"  Held by: pid {owner.pid}")
    suggestion = _suggest_free_port(host, port)
    if suggestion is not None:
        console.print(f"  Free port: try [cyan]quartz web --port {suggestion}[/cyan]")
    else:
        last = min(port + _PORT_SUGGEST_SPAN, 65535)
        console.print(
            f"  No free port in {port + 1}-{last} either: stop the process above, "
            "or pass [cyan]--port[/cyan] with a port you know is free."
        )


def _is_addr_in_use(exc: BaseException) -> bool:
    """Whether ``exc``, or whatever it was raised from, is a failed bind (`Errno 98`).

    uvicorn logs the bind error and then exits with plain ``SystemExit(1)``; the
    errno survives only in the chained ``__context__``, so walk the chain instead
    of trusting the exit code.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno == errno.EADDRINUSE:
            return True
        if "address already in use" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _handle_prune_cli(argv: list[str]) -> None:
    """Run `quartz prune`: delete aged-out kernel snapshots, keep every transcript."""
    from quartz.core.session import default_sessions_dir, prune_snapshots
    from quartz.settings import load_settings

    parser = argparse.ArgumentParser(
        prog="quartz prune",
        description=(
            "Delete kernel snapshots for sessions not touched in N days. "
            "Conversation transcripts are never removed."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Age in days before a snapshot may be removed "
            f"(default: snapshot_retention_days, {load_settings().snapshot_retention_days}). 0 prunes nothing."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without deleting anything.",
    )
    args = parser.parse_args(argv)

    days = load_settings().snapshot_retention_days if args.days is None else args.days
    if days < 0:
        console.print("[red]Error: --days must be 0 or greater.[/red]")
        sys.exit(2)

    root = default_sessions_dir()
    result = prune_snapshots(root, older_than_days=days, dry_run=args.dry_run)

    if args.dry_run:
        verb = "Would remove"
    else:
        verb = "Removed"

    if days == 0:
        console.print("Retention is 0 days, which disables pruning. Pass --days N to prune.")
        return

    if result.snapshots_removed == 0:
        console.print(
            f"No snapshots older than {days} day{'s' if days != 1 else ''} in {escape(str(root))}."
        )
        return

    console.print(
        f"{verb} [bold]{result.snapshots_removed}[/bold] snapshot"
        f"{'s' if result.snapshots_removed != 1 else ''} "
        f"older than {days} day{'s' if days != 1 else ''}, "
        f"freeing [bold]{result.freed_mb} MB[/bold]."
    )
    if result.errors:
        console.print(f"[yellow]{result.errors} snapshot(s) could not be removed.[/yellow]")
    console.print("Transcripts were kept: every session can still be resumed and replayed.")


def _handle_web_cli(argv: list[str]) -> None:
    """Run `quartz web`: the browser transport over the same core as the terminal."""
    parser = argparse.ArgumentParser(
        prog="quartz web",
        description="Serve the Quartz web client (one WebSocket over one AgentRuntime).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for the agent.")
    parser.add_argument("--open", action="store_true", help="Open the default browser once listening.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="API-only: serve no built client, for use with `npm run dev` in webapp/.",
    )
    args = parser.parse_args(argv)

    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        console.print(f"[red]Error: --cwd is not a directory: {escape(str(cwd))}[/red]")
        sys.exit(2)

    # Probe the bind before anything is built or served: a doomed server that
    # prints a URL and then dies reads as "Quartz started" for as long as it takes
    # uvicorn to fail, which is exactly the confusion this avoids.
    if not _can_bind(args.host, args.port):
        _report_port_in_use(args.host, args.port)
        sys.exit(1)

    # There is no auth by design (a local single-user tool), so a reachable bind
    # address is a real exposure. Say so before starting rather than after.
    if not _is_loopback(args.host):
        console.print(
            f"[bold yellow]Warning:[/bold yellow] binding {escape(args.host)} exposes Quartz "
            "to your network, and the web transport has no authentication. "
            "Use [cyan]--host 127.0.0.1[/cyan] unless you know what is in front of it."
        )

    from quartz.bootstrap import BootstrapOptions, build_runtime

    def build(session_id: str | None, session_cwd: Path | None = None) -> Any:
        """Build the runtime for a new session (``None``) or an existing one.

        A browser tab resuming history goes through the same bootstrap the terminal
        uses, so a web session is indistinguishable on disk from a terminal session
        -- apart from `origin`, which is what lets the browser's sidebar tell its
        own sessions apart from ones started at a prompt.

        `session_cwd` is the folder picked in the browser for a *new* session. It is
        ignored when resuming: a resumed session's directory is in its own log.
        """
        return build_runtime(
            BootstrapOptions(
                cwd=session_cwd or cwd,
                resume_id=session_id,
                headless=False,
                plain_output=False,
                origin="web",
            )
        )

    from quartz.web.app import create_app

    app = create_app(build=build, cwd=cwd, dev=args.dev)

    if args.open:
        _open_browser_when_ready(args.host, args.port)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        pass
    except (OSError, SystemExit) as exc:
        # The probe passed and the bind failed anyway -- a genuine race with another
        # process, or a platform where the probe is optimistic. Same message, same
        # exit, so a race reads exactly like a conflict found up front.
        if not _is_addr_in_use(exc):
            raise
        _report_port_in_use(args.host, args.port)
        sys.exit(1)


def _open_browser_when_ready(host: str, port: int, timeout: float = 15.0) -> None:
    """Open the browser once the port accepts connections, off the main thread.

    Polling rather than opening immediately because uvicorn binds after `run` is
    called: opening first would land the user on a connection-refused page.
    """
    import socket
    import threading
    import time
    import webbrowser

    target = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{target}:{port}/"

    def _wait() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                if probe.connect_ex((target, port)) == 0:
                    webbrowser.open(url)
                    return
            time.sleep(0.1)

    threading.Thread(target=_wait, name="quartz-web-open", daemon=True).start()
