"""Slash command registry, result model, and transport-neutral metadata."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "COMMAND_REGISTRY",
    "CommandHandler",
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


def get_transport(rt: AgentRuntime) -> str:
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
