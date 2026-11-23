"""Slash commands as data: the transport-independent half of command handling.

A command is a function over the runtime. It mutates what it must and returns a
`CommandResult`; it never prints. Drawing belongs to the transport -- `tui/commands.py`
turns a result into `rich` output, and the web transport (Phase 2) turns the same
result into a JSON frame -- so each command is implemented exactly once.

That split is the whole point: two implementations of a command is the failure
mode this module exists to prevent. Which is also why `core/` may not import
`rich` or `quartz.tui`; `tests/core/test_import_boundaries.py` enforces it.

Commands that need a terminal modal (a picker, the clipboard, an OAuth browser
flow) are still *registered here*, because the web transport must know they exist
and must be able to say "run this in the terminal". They return
`kind="needs_terminal"`; the terminal's real implementation lives in
`tui/commands.py` and is dispatched there, never in this module.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from quartz.core.loop import TurnUsage
from quartz.core.session import Session
from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from quartz.settings import (
    DEFAULT_BACKGROUND_CHECK_EVERY,
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    DEFAULT_PROGRESS_EVERY,
    DEFAULT_WEB_IDLE_HOURS,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
    validate_background_check_every,
    validate_progress_every,
    validate_tokens,
    validate_web_idle_hours,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "COMMAND_REGISTRY",
    "CommandKind",
    "CommandRegistry",
    "CommandResult",
    "SlashCommand",
    "reload_runtime_mcp",
    "serves_web",
]

CommandKind = Literal["text", "list", "error", "needs_terminal"]


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """What a command has to say, in a form every transport can render.

    ``kind`` is the only field a renderer must switch on:

    - ``text``: prose for the user. ``title`` non-empty means "put this in a titled
      frame" (the terminal draws a panel, the web a card); empty means bare prose.
    - ``list``: ``items`` of flat dicts. The shapes are documented per command,
      because a list is data first -- the terminal may draw it as a table and the
      web as rows, but neither invents the values.
    - ``error``: something the user asked for could not be done. ``message`` is the
      failure, ``body`` an optional hint on how to do it right.
    - ``needs_terminal``: the command exists but only the terminal can run it.
    """

    kind: CommandKind
    title: str = ""
    body: str = ""
    items: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    message: str = ""

    @classmethod
    def text(cls, body: str, title: str = "") -> CommandResult:
        return cls(kind="text", title=title, body=body)

    @classmethod
    def listing(cls, items: list[dict[str, Any]], *, body: str = "") -> CommandResult:
        return cls(kind="list", items=items, body=body)

    @classmethod
    def error(cls, message: str, body: str = "") -> CommandResult:
        return cls(kind="error", message=message, body=body)

    @classmethod
    def needs_terminal(cls, message: str) -> CommandResult:
        return cls(kind="needs_terminal", message=message)


CommandHandler = Callable[["AgentRuntime", str], "CommandResult | None"]

#: Set on a handler that answers outside a terminal too, even though its command is
#: registered `needs_terminal`. The flag stays because the *terminal* owns these
#: commands' richer form; this marks the ones whose transport-neutral answer is real
#: rather than a redirect, so a browser is never told to go and run something it just
#: ran. Read it through `serves_web`, never by name.
_SERVES_WEB_ATTR = "serves_web"


def serves_web(command: SlashCommand) -> bool:
    """Whether a command answers in a browser despite its `needs_terminal` flag."""
    return bool(getattr(command.handler, _SERVES_WEB_ATTR, False))


def _transport(rt: AgentRuntime) -> str:
    """Which transport is drawing. ``"cli"`` unless the runtime says otherwise."""
    return getattr(rt, "transport", "cli")


@dataclasses.dataclass
class SlashCommand:
    """A registered slash command with metadata and its transport-neutral handler.

    ``immediate`` means the command never waits on the kernel, so it is safe to run
    while a turn is in flight. That covers commands that only touch local state (a
    config file, an in-memory value) *and* commands that read a value the kernel
    already pushed to the client: ``/jobs`` answers from the cached ``bg_jobs``
    snapshot, so it is immediate too -- waiting behind the turn to show background
    work that is happening during that very turn is the opposite of useful.

    Commands that ask the kernel a question (``/vars``, ``/helpers``) are not
    immediate: the request would block on a busy kernel, so they queue behind the
    turn instead.

    ``exits`` is separate from the handler's result because leaving is a transport
    action, not something a command has to say.

    ``needs_terminal`` is metadata, not the source of truth: the handler's result
    says the same thing. It is here so a client can grey a command out before
    calling it.
    """

    name: str
    description: str
    handler: CommandHandler
    usage: str = ""
    aliases: list[str] = dataclasses.field(default_factory=list)
    immediate: bool = False
    exits: bool = False
    needs_terminal: bool = False


class CommandRegistry:
    """Registry for slash commands, enabling clean decoupling, registration, and discovery."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        description: str,
        usage: str = "",
        aliases: list[str] | None = None,
        immediate: bool = False,
        exits: bool = False,
        needs_terminal: bool = False,
    ) -> Callable[[CommandHandler], CommandHandler]:
        def decorator(fn: CommandHandler) -> CommandHandler:
            cmd = SlashCommand(
                name=name,
                description=description,
                handler=fn,
                usage=usage,
                aliases=aliases or [],
                immediate=immediate,
                exits=exits,
                needs_terminal=needs_terminal,
            )
            self._commands[name] = cmd
            for alias in cmd.aliases:
                self._aliases[alias] = name
            return fn

        return decorator

    def get(self, name: str) -> SlashCommand | None:
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def lookup(self, user_input: str) -> SlashCommand | None:
        """Resolve the command a line invokes without running it.

        The dock asks this before dispatching so it knows whether the input can run
        beside a live turn.
        """
        trimmed = user_input.strip()
        if not trimmed.startswith("/"):
            return None
        return self.get(trimmed.split(maxsplit=1)[0].lower())

    def list_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def to_completion_dict(self) -> dict[str, str]:
        return {cmd.name: cmd.description for cmd in self._commands.values()}

    def format_help(self, *, transport: str = "cli") -> str:
        """The one help text, for the terminal and for the browser's command list.

        The command list is identical in every transport, because the commands are.
        Only the keybinding block moves: they are listed here rather than in the TUI
        so that "what can I type" has a single answer, and `transport` picks which
        set of keys that answer is about. Defaulting to ``"cli"`` is what keeps the
        terminal's output byte-identical to what it has always printed.
        """
        lines: list[str] = []
        for cmd in self._commands.values():
            if cmd.name == "/help":
                continue
            cmd_str = f"{cmd.name} {cmd.usage}".strip()
            lines.append(f"{cmd_str:<17}{cmd.description}")
        keys = _WEB_KEYBINDINGS if transport == "web" else _CLI_KEYBINDINGS
        lines.extend(["", "Keybindings:", *keys])
        return "\n".join(lines)

    def handle(self, rt: AgentRuntime, user_input: str) -> CommandResult | None:
        """Run the command a line invokes, if any. ``None`` means "not a command"."""
        trimmed = user_input.strip()
        if not trimmed.startswith("/"):
            return None
        parts = trimmed.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        cmd = self.lookup(trimmed)
        if cmd is None:
            return None
        return cmd.handler(rt, arg)


COMMAND_REGISTRY = CommandRegistry()

#: The terminal's keys. Byte-identical to what `/help` has always printed: people
#: learn these by running `/help`, and a reflowed column is a real cost to them.
_CLI_KEYBINDINGS = [
    "  Enter            Submit prompt or steer running turn",
    "  Ctrl+J / \\+Enter Insert newline",
    "  Esc              Abort running turn or cancel modal/completion",
    "  Ctrl+C           Clear draft / abort turn / exit",
    "  Ctrl+D           Exit when input is empty",
    "  Ctrl+T           Toggle thinking trace",
    "  Ctrl+O           Toggle detail tool calls",
    "  @path            Attach or mention a file",
]

#: The browser's. Same headings, different keys: Ctrl+T and `@path` do nothing in a
#: page, and a help card full of dead keys is worse than no help at all.
_WEB_KEYBINDINGS = [
    "  Enter            Send the prompt, or steer the running turn",
    "  Shift+Enter      Insert a newline",
    "  Esc              Abort the running turn or close the palette",
    "  Up               Recall the previous prompt",
    "  /                Open the command palette",
]

# ---------------------------------------------------------------------------
# Commands only the terminal can run.
#
# The terminal's real implementations live in `tui/commands.py` (they open
# pickers, touch the clipboard, or drive an OAuth flow). These stubs exist so the
# command is discoverable everywhere else -- and so that a web client is told to
# say "run this in the terminal" instead of silently failing. Phase 4 replaces
# them with real web controls one at a time.
# ---------------------------------------------------------------------------


@COMMAND_REGISTRY.register(
    "/login",
    "Log in to a provider (OAuth or API key)",
    usage="[prov]",
    needs_terminal=True,
)
def _cmd_login(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /login in the terminal: it needs an interactive OAuth flow or a hidden API-key prompt."
    )


@COMMAND_REGISTRY.register(
    "/logout",
    "Log out and remove stored provider credentials",
    usage="[prov]",
    needs_terminal=True,
)
def _cmd_logout(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /logout in the terminal: it edits the credential store you are logged in from."
    )


def resolve_model_arg(arg: str, current_provider: str) -> tuple[str, str | None, str | None]:
    """Parse a user's `/model` argument into (provider, model, effort)."""
    parts = arg.strip().split()
    if not parts:
        return current_provider, None, None

    from quartz.providers.registry import PROVIDERS
    from quartz.providers.selection import _provider_ready

    all_efforts = {"low", "medium", "high", "max", "xhigh", "minimal"}

    if parts[0].lower() in PROVIDERS:
        provider = parts[0].lower()
        rest = parts[1:]
        if not rest:
            return provider, None, None
        if len(rest) >= 2 and rest[-1].lower() in all_efforts:
            return provider, " ".join(rest[:-1]), rest[-1].lower()
        return provider, " ".join(rest), None

    if len(parts) >= 2 and parts[-1].lower() in all_efforts:
        model_part = " ".join(parts[:-1])
        effort = parts[-1].lower()
    else:
        model_part = " ".join(parts)
        effort = None

    if "/" in model_part:
        prefix, remainder = model_part.split("/", 1)
        if prefix.lower() in PROVIDERS:
            return prefix.lower(), remainder, effort

    if model_part.startswith("gemini-"):
        return "google", model_part, effort
    if model_part.startswith(("gpt-", "o1", "o3", "o4")):
        if _provider_ready("openai"):
            return "openai", model_part, effort
        if _provider_ready("openrouter"):
            return "openrouter", model_part, effort
        return "openai", model_part, effort
    if model_part.startswith("deepseek-"):
        if _provider_ready("deepseek"):
            return "deepseek", model_part, effort
        if _provider_ready("openrouter"):
            return "openrouter", model_part, effort
        return "deepseek", model_part, effort
    if "/" in model_part:
        return "openrouter", model_part, effort

    return current_provider, model_part, effort


def apply_model_switch(
    rt: AgentRuntime,
    provider: str,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[bool, str]:
    """Switch the runtime to a new provider/model/effort, persisting to config."""
    from quartz.errors import CredentialError, EndpointConfigError
    from quartz.providers.registry import resolve_context_tokens, save_model_config
    from quartz.providers.selection import build_provider

    old_client = rt.client
    try:
        new_client, pname, new_model, new_effort = build_provider(
            provider=provider,
            model_override=model,
            effort_override=effort,
        )
    except (CredentialError, EndpointConfigError, ValueError) as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Failed to switch model: {exc}"

    rt.client = new_client
    rt.provider_name = pname
    rt.model = new_model
    rt.effort = new_effort

    if old_client is not None and old_client is not rt.client:
        close_fn = getattr(old_client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    try:
        save_model_config(rt.provider_name, rt.model, rt.effort)
    except Exception:
        pass

    try:
        rt.max_context_tokens = resolve_context_tokens(rt.provider_name, rt.model, rt.client)
    except Exception:
        pass

    return True, f"Switched to {rt.provider_name} / {rt.model} (effort: {rt.effort})."


@COMMAND_REGISTRY.register(
    "/model",
    "Choose provider, model, and reasoning effort",
    usage="[model] [effort]",
    needs_terminal=True,
)
def _cmd_model(rt: AgentRuntime, arg: str) -> CommandResult | None:
    trimmed = arg.strip()
    if trimmed:
        provider, model, effort = resolve_model_arg(trimmed, rt.provider_name)
        ok, msg = apply_model_switch(rt, provider, model, effort)
        if not ok:
            return CommandResult.error(f"Failed to switch model: {msg}")
        return CommandResult.text(msg, title="/model")

    if _transport(rt) == "web":
        return CommandResult.text(
            f"Active model: **{rt.model}** ({rt.provider_name}, effort: {rt.effort}).\n\n"
            f"Use the model picker in the header or run `/model <name> [effort]` to switch.",
            title="/model",
        )
    return CommandResult.needs_terminal(
        "Run /model in the terminal to pick provider, model, and reasoning effort."
    )


setattr(_cmd_model, _SERVES_WEB_ATTR, True)


@COMMAND_REGISTRY.register(
    "/continue",
    "Pick and resume a previous session in this directory",
    usage="[id]",
    needs_terminal=True,
)
def _cmd_continue(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /continue in the terminal to pick and resume a previous session."
    )


@COMMAND_REGISTRY.register(
    "/image",
    "Attach an image file (or /image clear)",
    usage="[path]",
    needs_terminal=True,
)
def _cmd_image(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /image in the terminal to attach a file from the local filesystem."
    )


@COMMAND_REGISTRY.register(
    "/paste",
    "Attach an image from the system clipboard",
    needs_terminal=True,
)
def _cmd_paste(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /paste in the terminal: the system clipboard is not readable from a browser."
    )


# ---------------------------------------------------------------------------
# Commands with no front end of their own. Each returns data; the transport draws.
# ---------------------------------------------------------------------------


@COMMAND_REGISTRY.register(
    "/vars",
    "List live Python variables in the kernel",
)
def _cmd_vars(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    vars_info = rt.kernel.list_vars()
    if not vars_info:
        return CommandResult.text("No user-defined variables in the kernel.")
    return CommandResult.listing([{"name": name, "type": typ} for name, typ in vars_info.items()])


@COMMAND_REGISTRY.register(
    "/helpers",
    "List available REPL helper functions (built-in and custom)",
)
def _cmd_helpers(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    from quartz.kernel.helpers import load_helpers

    if rt.kernel is not None:
        data = rt.kernel.list_helpers()
        helpers_list = data.get("helpers", [])
        warnings = data.get("warnings", [])
    else:
        loaded = load_helpers(cwd=rt.cwd)
        helpers_list = [
            {
                "name": h.name,
                "source": h.source,
                "signature": h.signature,
                "doc": h.doc,
                "file_path": h.file_path,
            }
            for h in loaded.infos.values()
        ]
        warnings = loaded.warnings

    # Builtins first, then custom helpers alphabetically: the order is part of the
    # answer, so it is decided here rather than by whichever transport draws it.
    items = [
        {
            "name": h.get("name", ""),
            "source": h.get("source", ""),
            "signature": h.get("signature", ""),
            "doc": h.get("doc", ""),
        }
        for h in sorted(
            helpers_list, key=lambda x: (x.get("source") != "builtin", x.get("name", ""))
        )
    ]
    body = "\n".join(f"Warning: {w}" for w in warnings)
    return CommandResult.listing(items, body=body)


@COMMAND_REGISTRY.register(
    "/session",
    "Show active session ID and file paths",
    needs_terminal=True,
)
def _cmd_session(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    if _transport(rt) == "web":
        # The browser has no terminal to send the reader to, and these three values
        # *are* the answer: printing them beats telling someone to go and ask again
        # somewhere else. The paths are the server's own -- the browser cannot open
        # them, but a person debugging where a session lives can read them.
        return CommandResult.text(
            f"Session ID: {rt.active_session.id}\n"
            f"Log: {rt.active_session.jsonl_path}\n"
            f"Snapshot: {rt.active_session.snapshot_dir}"
        )
    return CommandResult.needs_terminal(
        "Run /session in the terminal to print the session ID, log path, and snapshot path."
    )


# The registration above keeps `needs_terminal`: the *terminal* has the richer form of
# this command. But the answer above is real in a browser, not a redirect, so a web
# client must not badge it as terminal-only -- `serves_web` is what tells it apart
# from the commands that genuinely have nowhere else to go.
setattr(_cmd_session, _SERVES_WEB_ATTR, True)


@COMMAND_REGISTRY.register(
    "/usage",
    "Show context window usage and cache hit rate",
)
def _cmd_usage(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    pct = rt.usage_stats.context_pct(rt.max_context_tokens)
    cache_pct = rt.usage_stats.cache_hit_rate * 100.0
    total_cache = (
        (rt.usage_stats.total_cached_tokens / rt.usage_stats.total_prompt_tokens * 100.0)
        if rt.usage_stats.total_prompt_tokens > 0
        else 0.0
    )
    overflow_note = ""
    if rt.usage_stats.is_overflowing(rt.max_context_tokens):
        overflow_note = (
            " ⚠ OVERFLOW: last prompt exceeds this model's context window"
            " (stale denominator or window too small — /compact or /model)"
        )
    return CommandResult.text(
        f"Last context:  {rt.usage_stats.prompt_tokens:,} / {rt.max_context_tokens:,} ({pct:.1f}%)\n"
        f"Last cache (CH):    {rt.usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
        f"Session total: {rt.usage_stats.total_prompt_tokens:,} in / "
        f"{rt.usage_stats.total_completion_tokens:,} out / "
        f"{rt.usage_stats.total_cached_tokens:,} cached ({total_cache:.1f}%)"
        + overflow_note
    )


def parse_expand_range(arg: str, total: int) -> tuple[int, int]:
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


@COMMAND_REGISTRY.register(
    "/expand",
    "Expand a collapsed ipython cell (default: latest)",
    usage="[n|a..b]",
    aliases=["/last"],
)
def _cmd_expand(rt: AgentRuntime, arg: str) -> CommandResult | None:
    if not rt.cell_history:
        return CommandResult.text("No ipython cells executed in this session yet.")
    try:
        start_idx, end_idx = parse_expand_range(arg, len(rt.cell_history))
    except ValueError:
        return CommandResult.error(
            f"Invalid cell index or range. Choose 1..{len(rt.cell_history)}."
        )
    items = []
    for idx in range(start_idx, end_idx + 1):
        code, res, duration = rt.cell_history[idx - 1]
        items.append(
            {
                "cell": idx,
                "duration_s": duration,
                "ok": res.ok,
                "code": code,
                "output": res.format_for_model().strip(),
            }
        )
    return CommandResult.listing(items)


@COMMAND_REGISTRY.register(
    "/compact",
    "Summarize older messages while keeping the kernel alive",
    usage="[focus]",
)
def _cmd_compact(rt: AgentRuntime, arg: str) -> CommandResult | None:
    result = rt.compact(focus=arg or None)
    if not result.changed:
        return CommandResult.text("Nothing to compact yet.")
    # The numbers travel as a list so a renderer that wants the compaction line
    # (the terminal does) has the values, instead of having to re-read the runtime
    # after the fact and guess what changed.
    return CommandResult(
        kind="text",
        body=f"Context is now ~{result.estimated_tokens_after:,} tokens.",
        items=[
            {
                "reason": result.reason.value,
                "summarized": result.summarized,
                "kept": result.kept,
                "cleared": result.cleared,
                "tokens_before": result.estimated_tokens_before,
                "tokens_after": result.estimated_tokens_after,
            }
        ],
    )


#: Accepted spellings per setting. The config file's exact key works, because that
#: is what someone who opened `~/.quartz/config.json` will type, and so does the
#: short form `/settings` prints.
_SETTING_ALIASES: dict[str, str] = {
    "reserve": "reserve",
    "reserve_tokens": "reserve",
    "compact_reserve_tokens": "reserve",
    "keep": "keep",
    "keep_recent": "keep",
    "keep_recent_tokens": "keep",
    "progress": "progress",
    "progress_every": "progress",
    "bg": "bg",
    "background": "bg",
    "background_check_every": "bg",
    "idle": "idle",
    "web_idle_hours": "idle",
}

_SETTINGS_USAGE = (
    "Usage: /settings reserve <tokens> | /settings keep <tokens> | "
    "/settings progress <n> | /settings bg <seconds> | /settings idle <hours> | "
    "/settings reset | /settings list"
)


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep <tokens>|progress <n>|bg <seconds>|idle <hours>|reset|list]",
    immediate=True,
)
def _cmd_settings(rt: AgentRuntime, arg: str) -> CommandResult | None:
    # Local import: `quartz.providers.registry` pulls in the provider layer, which
    # a transport that never shows /settings should not have to import.
    from quartz.providers.registry import config_file_path

    stored_settings = load_settings()
    current = Settings(
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
        progress_every=rt.progress_every,
        background_check_every=rt.background_check_every,
        web_idle_hours=stored_settings.web_idle_hours,
    )
    parts = (arg or "").split()
    if parts:
        parts[0] = _SETTING_ALIASES.get(parts[0], parts[0])

    if not parts:
        return CommandResult.text(
            f"reserve_tokens: {current.compact_reserve_tokens:,} "
            f"(default {DEFAULT_COMPACT_RESERVE_TOKENS:,})\n"
            f"keep_recent_tokens: {current.keep_recent_tokens:,} "
            f"(default {DEFAULT_KEEP_RECENT_TOKENS:,})\n"
            f"progress_every: {current.progress_every} "
            f"(default {DEFAULT_PROGRESS_EVERY}, 0 disables; off in -p)\n"
            f"background_check_every: {current.background_check_every}s "
            f"(default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables; off in -p)\n"
            f"web_idle_hours: {current.web_idle_hours}h "
            f"(default {DEFAULT_WEB_IDLE_HOURS}h, 0 disables)\n\n"
            f"Stored in {config_file_path()}\n\n"
            f"{_SETTINGS_USAGE}",
            title="Settings",
        )

    if parts[0] == "list":
        try:
            stored = json.dumps(settings_as_dict(load_settings()), indent=2)
        except Exception:
            stored = json.dumps(settings_as_dict(current), indent=2)
        return CommandResult.text(stored, title="Settings (stored)")

    if parts[0] == "reset":
        defaults = Settings()
        save_settings(defaults)
        rt.compact_reserve_tokens = defaults.compact_reserve_tokens
        rt.keep_recent_tokens = defaults.keep_recent_tokens
        rt.progress_every = defaults.progress_every
        rt.background_check_every = defaults.background_check_every
        return CommandResult.text(
            f"Reset settings to defaults. "
            f"reserve_tokens = {defaults.compact_reserve_tokens:,}, "
            f"keep_recent_tokens = {defaults.keep_recent_tokens:,}, "
            f"progress_every = {defaults.progress_every}, "
            f"background_check_every = {defaults.background_check_every}s."
        )

    if parts[0] == "progress" and len(parts) == 2:
        try:
            every = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer call count.",
                body="Usage: /settings progress <n> (0 disables updates)",
            )
        if not validate_progress_every(every):
            return CommandResult.error(
                f"Invalid value {every}: must be between 0 and 1,000 calls.",
                body="Usage: /settings progress <n> (0 disables updates)",
            )
        save_settings(
            Settings(
                compact_reserve_tokens=current.compact_reserve_tokens,
                keep_recent_tokens=current.keep_recent_tokens,
                progress_every=every,
                background_check_every=current.background_check_every,
                web_idle_hours=current.web_idle_hours,
            )
        )
        rt.progress_every = every
        state = "off" if every == 0 else f"every {every} ipython calls"
        return CommandResult.text(
            f"Saved. progress_every = {every} ({state}). Applies from your next message."
        )

    if parts[0] == "bg" and len(parts) == 2:
        try:
            every = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected a number of seconds.",
                body="Usage: /settings bg <seconds> (0 disables status checks)",
            )
        if not validate_background_check_every(every):
            return CommandResult.error(
                f"Invalid value {every}: must be between 0 and 86,400 seconds.",
                body="Usage: /settings bg <seconds> (0 disables status checks)",
            )
        save_settings(
            Settings(
                compact_reserve_tokens=current.compact_reserve_tokens,
                keep_recent_tokens=current.keep_recent_tokens,
                progress_every=current.progress_every,
                background_check_every=every,
                web_idle_hours=current.web_idle_hours,
            )
        )
        rt.background_check_every = every
        # The worker owns the clock, so it has to be told; a restart re-applies it.
        rt.kernel.configure(background_check_every=every)
        state = "off" if every == 0 else f"every {every}s while a job runs"
        return CommandResult.text(
            f"Saved. background_check_every = {every}s ({state}). Applies from your next message."
        )

    if parts[0] in ("reserve", "keep") and len(parts) == 2:
        try:
            value = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer token count.",
                body="Usage: /settings reserve <tokens> | /settings keep <tokens>",
            )
        if not validate_tokens(value):
            return CommandResult.error(
                f"Invalid value {value:,}: must be between 256 and 10,000,000 tokens.",
                body="Usage: /settings reserve <tokens> | /settings keep <tokens>",
            )
        if parts[0] == "reserve":
            updated = Settings(
                compact_reserve_tokens=value,
                keep_recent_tokens=current.keep_recent_tokens,
                progress_every=current.progress_every,
                background_check_every=current.background_check_every,
                web_idle_hours=current.web_idle_hours,
            )
            rt.compact_reserve_tokens = value
            label = "reserve_tokens"
        else:
            updated = Settings(
                compact_reserve_tokens=current.compact_reserve_tokens,
                keep_recent_tokens=value,
                progress_every=current.progress_every,
                background_check_every=current.background_check_every,
                web_idle_hours=current.web_idle_hours,
            )
            rt.keep_recent_tokens = value
            label = "keep_recent_tokens"
        save_settings(updated)
        return CommandResult.text(
            f"Saved. {label} = {value:,}. Applies from your next message."
        )

    if parts[0] == "idle" and len(parts) == 2:
        try:
            hours = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer hour count.",
                body="Usage: /settings idle <hours> (0 disables idle prompts)",
            )
        if not validate_web_idle_hours(hours):
            return CommandResult.error(
                f"Invalid value {hours}: must be between 0 and 8,760 hours.",
                body="Usage: /settings idle <hours> (0 disables idle prompts)",
            )
        save_settings(
            Settings(
                compact_reserve_tokens=current.compact_reserve_tokens,
                keep_recent_tokens=current.keep_recent_tokens,
                progress_every=current.progress_every,
                background_check_every=current.background_check_every,
                web_idle_hours=hours,
            )
        )
        state = "off" if hours == 0 else f"every {hours}h"
        return CommandResult.text(
            f"Saved. web_idle_hours = {hours}h ({state})."
        )

    return CommandResult.error(f"Unknown settings argument {arg!r}.", body=_SETTINGS_USAGE)


@COMMAND_REGISTRY.register(
    "/prompt",
    "Show the assembled system prompt",
)
def _cmd_prompt(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    return CommandResult.text(rt.system_prompt, title="System Prompt")


@COMMAND_REGISTRY.register(
    "/reset",
    "Clear history, start a new session, and restart the kernel",
)
def _cmd_reset(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    rt.messages.clear()
    rt.cell_history.clear()
    rt.pending_images.clear()
    rt.usage_stats = TurnUsage()
    rt.total_elapsed_s = 0.0
    rt.turn_count = 0
    rt.wait_snapshot()
    rt.kernel.restart()
    # Same origin as the session being replaced: `/reset` in the browser must not
    # hand the user a session the browser's own sidebar then refuses to list.
    rt.active_session = Session.create(
        rt.cwd, sessions_dir=rt.active_session.sessions_dir, origin=rt.active_session.origin
    )
    return CommandResult.text("Conversation cleared, kernel restarted.")


@COMMAND_REGISTRY.register(
    "/jobs",
    "List running background bash() jobs with elapsed time and output tail",
    immediate=True,
)
def _cmd_jobs(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    try:
        jobs_fn = getattr(rt.kernel, "running_bash_jobs", None)
        jobs = list(jobs_fn()) if callable(jobs_fn) else []
    except Exception:
        jobs = []
    if not jobs:
        return CommandResult.text("No background jobs running.")
    # The human label ("Run tests") needs the shell classifier, which lives next to
    # the kernel; the transport that wants prose builds it. Raw is the better
    # contract: a web client can show the command itself.
    return CommandResult.listing(
        [
            {
                "pid": job.get("pid", "?"),
                "command": str(job.get("command", "")),
                "elapsed_s": float(job.get("elapsed_s", 0.0) or 0.0),
            }
            for job in jobs
        ]
    )


def reload_runtime_mcp(rt: AgentRuntime) -> None:
    """Close cached MCP sessions in the active kernel and refresh system_prompt."""
    try:
        exec_fn = getattr(rt.kernel, "exec_cell", None)
        if callable(exec_fn):
            exec_fn("await mcp.close()", timeout=5.0)
    except Exception:
        pass
    try:
        from quartz.core.prompt import build_system_prompt
        from quartz.settings import is_project_trusted

        trusted = is_project_trusted(rt.cwd)
        rt.system_prompt = build_system_prompt(rt.cwd, trusted=trusted)
    except Exception:
        pass


def _format_mcp_detail(spec: dict[str, Any]) -> str:
    enabled = spec.get("enabled", True) is not False
    status = "enabled" if enabled else "disabled"
    if "url" in spec:
        target = f"http: {spec['url']}"
    else:
        cmd = str(spec.get("command", ""))
        args = " ".join(str(a) for a in spec.get("args", []))
        target = f"stdio: {cmd} {args}".strip()
    return f"[{status}] {target}"


def _parse_mcp_add_tokens(tokens: list[str]) -> tuple[str, dict[str, Any]]:
    if not tokens:
        raise ValueError("Usage: /mcp add <name> [--url URL | -- <cmd> ...]")
    name = tokens[0]
    url: str | None = None
    headers: dict[str, str] = {}
    env: dict[str, str] = {}
    cwd: str | None = None
    disabled = False
    cmd_parts: list[str] = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            cmd_parts = tokens[i + 1 :]
            break
        if tok == "--disabled":
            disabled = True
            i += 1
            continue
        if tok in ("--url", "--header", "--env", "--cwd"):
            if i + 1 >= len(tokens):
                raise ValueError(f"Missing value after {tok}")
            val = tokens[i + 1]
            if tok == "--url":
                url = val
            elif tok == "--cwd":
                cwd = val
            elif tok in ("--header", "--env"):
                if "=" not in val:
                    raise ValueError(f"{tok} expects KEY=VALUE")
                k, v = val.split("=", 1)
                if tok == "--header":
                    headers[k.strip()] = v
                else:
                    env[k.strip()] = v
            i += 2
            continue
        cmd_parts = tokens[i:]
        break

    spec: dict[str, Any] = {}
    if url:
        spec["url"] = url
        if headers:
            spec["headers"] = headers
    elif cmd_parts:
        spec["command"] = cmd_parts[0]
        if len(cmd_parts) > 1:
            spec["args"] = cmd_parts[1:]
        if env:
            spec["env"] = env
        if cwd:
            spec["cwd"] = cwd
    else:
        raise ValueError("Provide --url <url> or -- <command> [args...]")

    if disabled:
        spec["enabled"] = False
    return name, spec


@COMMAND_REGISTRY.register(
    "/mcp",
    "List, add, remove, enable, or disable MCP servers",
    usage="[list|enable|disable|remove|add ...]",
)
def _cmd_mcp(rt: AgentRuntime, arg: str) -> CommandResult | None:
    try:
        tokens = shlex.split(arg) if arg.strip() else []
    except ValueError as exc:
        return CommandResult.error(f"Invalid arguments: {exc}")

    cfg_path = default_mcp_config_path()
    action = tokens[0].lower() if tokens else "list"
    rest = tokens[1:]

    if action == "list":
        servers = load_raw_mcp_config(cfg_path)
        if not servers:
            return CommandResult.text(f"No MCP servers configured in {cfg_path}.")
        return CommandResult.listing(
            [
                {"label": name, "detail": _format_mcp_detail(spec)}
                for name, spec in sorted(servers.items())
            ]
        )

    if action in ("enable", "disable"):
        target_enabled = action == "enable"
        if "--all" in rest:
            targets = sorted(load_raw_mcp_config(cfg_path).keys())
        else:
            targets = [t for t in rest if not t.startswith("-")]
        if not targets:
            return CommandResult.error(f"Usage: /mcp {action} <name...> | --all")
        missing: list[str] = []
        for name in targets:
            if not set_mcp_server_enabled(name, target_enabled, cfg_path):
                missing.append(name)
        updated = [n for n in targets if n not in missing]
        if updated:
            reload_runtime_mcp(rt)
        verb = "Enabled" if target_enabled else "Disabled"
        if missing and not updated:
            return CommandResult.error(f"MCP server(s) not found: {', '.join(missing)}")
        msg = f"{verb} MCP server(s): {', '.join(updated)}."
        if missing:
            msg += f" Not found: {', '.join(missing)}."
        return CommandResult.text(msg)

    if action == "remove":
        if not rest:
            return CommandResult.error("Usage: /mcp remove <name>")
        name = rest[0]
        if not remove_mcp_server(name, cfg_path):
            return CommandResult.error(f"MCP server '{name}' not found.")
        reload_runtime_mcp(rt)
        return CommandResult.text(f"Removed MCP server '{name}'.")

    if action == "add":
        try:
            name, spec = _parse_mcp_add_tokens(rest)
        except ValueError as exc:
            return CommandResult.error(str(exc))
        add_mcp_server(name, spec, cfg_path)
        reload_runtime_mcp(rt)
        return CommandResult.text(f"Saved MCP server '{name}'.")

    return CommandResult.error(
        "Unknown /mcp action. Use: list, enable, disable, remove, or add."
    )


@COMMAND_REGISTRY.register(
    "/help",
    "Show available slash commands",
    immediate=True,
)
def _cmd_help(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    return CommandResult.text(COMMAND_REGISTRY.format_help(transport=_transport(rt)))


@COMMAND_REGISTRY.register(
    "/exit",
    "Exit the agent",
    aliases=["/quit"],
    exits=True,
)
def _cmd_exit(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    # Leaving is the transport's decision, so a command that exits says nothing.
    return None
