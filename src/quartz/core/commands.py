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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from quartz.core.loop import TurnUsage
from quartz.core.session import Session
from quartz.settings import (
    DEFAULT_BACKGROUND_CHECK_EVERY,
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    DEFAULT_PROGRESS_EVERY,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
    validate_background_check_every,
    validate_progress_every,
    validate_tokens,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "COMMAND_REGISTRY",
    "CommandKind",
    "CommandRegistry",
    "CommandResult",
    "SlashCommand",
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


@COMMAND_REGISTRY.register(
    "/model",
    "Choose provider, model, and reasoning effort",
    needs_terminal=True,
)
def _cmd_model(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /model in the terminal to pick provider, model, and reasoning effort."
    )


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
}

_SETTINGS_USAGE = (
    "Usage: /settings reserve <tokens> | /settings keep <tokens> | "
    "/settings progress <n> | /settings bg <seconds> | /settings reset | /settings list"
)


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep <tokens>|progress <n>|bg <seconds>|reset|list]",
    immediate=True,
)
def _cmd_settings(rt: AgentRuntime, arg: str) -> CommandResult | None:
    # Local import: `quartz.providers.registry` pulls in the provider layer, which
    # a transport that never shows /settings should not have to import.
    from quartz.providers.registry import config_file_path

    current = Settings(
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
        progress_every=rt.progress_every,
        background_check_every=rt.background_check_every,
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
            f"(default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables; off in -p)\n\n"
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
            )
            rt.compact_reserve_tokens = value
            label = "reserve_tokens"
        else:
            updated = Settings(
                compact_reserve_tokens=current.compact_reserve_tokens,
                keep_recent_tokens=value,
                progress_every=current.progress_every,
                background_check_every=current.background_check_every,
            )
            rt.keep_recent_tokens = value
            label = "keep_recent_tokens"
        save_settings(updated)
        return CommandResult.text(
            f"Saved. {label} = {value:,}. Applies from your next message."
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
