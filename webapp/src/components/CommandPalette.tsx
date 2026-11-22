import * as React from "react";
import { Input, Kbd, Modal, useOverlayState } from "@heroui/react";
import { CornerDownLeft, Search } from "lucide-react";
import { cn } from "@heroui/react";
import { handOffPrompt, isProseQuery } from "@/lib/commandText";
import type { Command } from "@/protocol";

/**
 * Subsequence match, scoring prefix hits highest. Small enough to need no library
 * and predictable enough to be worth owning: the ranking is part of the UX.
 */
export function fuzzyScore(query: string, target: string): number {
  if (query === "") return 0;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (t.startsWith(q)) return 1000 - t.length;
  const index = t.indexOf(q);
  if (index !== -1) return 500 - index - t.length;

  // Subsequence: every query char in order, gaps penalised.
  let cursor = 0;
  let gaps = 0;
  for (const char of q) {
    const found = t.indexOf(char, cursor);
    if (found === -1) return -1;
    gaps += found - cursor;
    cursor = found + 1;
  }
  return 100 - gaps;
}

export function filterCommands(commands: readonly Command[], query: string): Command[] {
  const scored = commands
    .map((command) => ({
      command,
      score: Math.max(
        fuzzyScore(query, command.name),
        fuzzyScore(query, command.description) - 50,
      ),
    }))
    .filter((entry) => entry.score >= 0);
  scored.sort((a, b) => b.score - a.score || a.command.name.localeCompare(b.command.name));
  return scored.map((entry) => entry.command);
}

export function parseCommandInput(input: string): { name: string; arg: string } {
  const trimmed = input.startsWith("/") ? input.slice(1) : input;
  const space = trimmed.indexOf(" ");
  if (space === -1) return { name: trimmed, arg: "" };
  return { name: trimmed.slice(0, space), arg: trimmed.slice(space + 1).trim() };
}

/**
 * Absent means "works here": a server that predates the field does not send it,
 * and guessing that its commands need a terminal would bury all of them.
 */
export function isTerminalOnly(command: Command): boolean {
  return command.needsTerminal === true;
}

/**
 * Commands that only the terminal can run sink below the ones that work here.
 *
 * They stay in the list -- hiding them would make the web transport look like it
 * lost half the app, and running one explains itself -- but they are not peers of
 * `/usage`, so they are marked and ranked after it. This is a stable partition of
 * the fuzzy ranking, not a second score: the order *within* each group is exactly
 * what `filterCommands` produced.
 */
export function orderCommands(commands: readonly Command[]): Command[] {
  return [...commands].sort((a, b) => Number(isTerminalOnly(a)) - Number(isTerminalOnly(b)));
}

/**
 * The command a query is filling in, once an argument has been started.
 *
 * `/compact 4000` matches nothing (the row is `/compact`), so as soon as the
 * person types a space the list can no longer tell them what the command takes.
 * The palette says it instead, above the footer, which is where someone who is
 * about to guess the token count looks.
 */
export function usageHint(commands: readonly Command[], query: string): Command | null {
  const gap = /\s/.exec(query);
  if (gap === null) return null;
  const name = query.slice(0, gap.index).toLowerCase();
  return commands.find((command) => command.name.toLowerCase() === name) ?? null;
}

export interface CommandPaletteProps {
  open: boolean;
  commands: readonly Command[];
  onOpenChange: (open: boolean) => void;
  onRun: (name: string, arg?: string) => void;
}

/**
 * Command palette.
 *
 * A list, a filter, Enter. Commands that need a terminal still appear - hiding
 * them would make the web transport feel like it lost half the app - but they are
 * marked and ranked below the ones that work here, and they report back what to run
 * instead.
 *
 * It also owns the first few keystrokes of any `/` draft, which is what makes a
 * prompt that starts with a slash typeable: as soon as the text cannot be a
 * command (see `isProseQuery`), the palette closes and hands it back to the
 * composer as ordinary text.
 */
export function CommandPalette({
  open,
  commands,
  onOpenChange,
  onRun,
}: CommandPaletteProps): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  const commandNames = React.useMemo(() => commands.map((command) => command.name), [commands]);

  const filtered = React.useMemo(
    () => orderCommands(filterCommands(commands, query)),
    [commands, query],
  );

  const hint = React.useMemo(() => usageHint(commands, query), [commands, query]);

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
  }, [open]);

  React.useEffect(() => {
    if (!open) return;
    // The filter is the only thing worth typing into; make it the focus target
    // once the dialog has mounted rather than relying on tab order.
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  React.useEffect(() => {
    setActive(0);
  }, [query]);

  const onFilterChange = React.useCallback(
    (value: string) => {
      if (isProseQuery(value, commandNames)) {
        // Not a command after all: give the text back to the composer whole, with
        // the slash this palette was opened by, instead of running it as an
        // unknown command nobody asked for.
        handOffPrompt(value);
        onOpenChange(false);
        return;
      }
      setQuery(value);
    },
    [commandNames, onOpenChange],
  );

  const run = React.useCallback(
    (command: Command | undefined) => {
      if (!command) return;
      onRun(command.name);
      onOpenChange(false);
    },
    [onOpenChange, onRun],
  );

  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive((prev) => (filtered.length === 0 ? 0 : (prev + 1) % filtered.length));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive((prev) =>
          filtered.length === 0 ? 0 : (prev - 1 + filtered.length) % filtered.length,
        );
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const selected = filtered[active];
        if (selected) {
          run(selected);
        } else if (query.trim() !== "") {
          // Free-form: `/compact 4000` is not in the list until it is typed.
          const parsed = parseCommandInput(query);
          if (parsed.name !== "") {
            onRun(parsed.name, parsed.arg === "" ? undefined : parsed.arg);
            onOpenChange(false);
          }
        }
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        const selected = filtered[active];
        if (selected) setQuery(`${selected.name} `);
      }
    },
    [active, filtered, onOpenChange, onRun, query, run],
  );

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="md" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">Commands</Modal.Heading>
            </Modal.Header>

            <div className="flex items-center gap-2 px-4 py-3">
              <Search aria-hidden="true" className="size-4 shrink-0 text-muted" />
              <Input
                ref={inputRef}
                value={query}
                onChange={(event) => onFilterChange(event.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Search commands…"
                aria-label="Filter commands"
                role="combobox"
                aria-expanded="true"
                aria-controls="command-list"
                aria-autocomplete="list"
                aria-activedescendant={
                  filtered[active] ? `command-option-${filtered[active].name}` : undefined
                }
                spellCheck={false}
                autoComplete="off"
                autoFocus
                className="border-0 bg-transparent px-0 shadow-none"
              />
            </div>

            <Modal.Body className="max-h-[50vh] overflow-y-auto px-2 pb-2">
              <ul id="command-list" role="listbox" aria-label="Available commands">
                {filtered.length === 0 ? (
                  <li className="px-3 py-6 text-center text-sm text-muted">
                    No command matches “{query}”.
                  </li>
                ) : null}
                {filtered.map((command, index) => (
                  <li
                    key={command.name}
                    id={`command-option-${command.name}`}
                    role="option"
                    aria-selected={index === active}
                    data-selected={index === active}
                    className={cn(
                      "flex cursor-pointer items-baseline gap-2 rounded-lg px-3 py-2",
                      index === active ? "bg-accent-soft text-accent-soft-foreground" : "text-foreground",
                    )}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => run(command)}
                  >
                    <span className="shrink-0 font-mono text-sm font-medium">/{command.name}</span>
                    {command.usage ? (
                      <span className="shrink-0 font-mono text-xs text-muted">{command.usage}</span>
                    ) : null}
                    <span className="min-w-0 truncate text-xs text-muted">{command.description}</span>
                    {isTerminalOnly(command) ? (
                      <span
                        title="Only the terminal can run this"
                        className="shrink-0 rounded border border-border px-1 text-[0.625rem] tracking-wide text-muted uppercase"
                      >
                        terminal
                      </span>
                    ) : null}
                    {index === active ? (
                      <CornerDownLeft
                        aria-hidden="true"
                        className="ml-auto size-3.5 shrink-0 self-center text-muted"
                      />
                    ) : null}
                  </li>
                ))}
              </ul>
            </Modal.Body>

            {hint !== null ? (
              <p className="flex flex-wrap items-baseline gap-x-2 border-t border-separator px-4 py-2 text-xs">
                <span className="text-muted">Usage</span>
                <span className="font-mono text-foreground">
                  /{hint.name}
                  {hint.usage ? ` ${hint.usage}` : ""}
                </span>
                {isTerminalOnly(hint) ? <span className="text-muted">(terminal only)</span> : null}
                <span className="text-muted">— Enter runs what you typed</span>
              </p>
            ) : null}

            <Modal.Footer className="border-t border-separator px-4 py-2">
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                <span className="flex items-center gap-1">
                  <Kbd>↑</Kbd>
                  <Kbd>↓</Kbd>
                  to move
                </span>
                <span className="flex items-center gap-1">
                  <Kbd>Enter</Kbd> to run
                </span>
                <span className="flex items-center gap-1">
                  <Kbd>Esc</Kbd> to close
                </span>
              </p>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}