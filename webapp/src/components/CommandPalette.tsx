import * as React from "react";
import { CornerDownLeft, Terminal } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";
import { cn } from "@/lib/utils";
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

export interface CommandPaletteProps {
  open: boolean;
  commands: readonly Command[];
  onOpenChange: (open: boolean) => void;
  onRun: (name: string, arg?: string) => void;
}

/**
 * Command palette.
 *
 * A list, a filter, Enter. Commands that need a terminal still appear — hiding
 * them would make the web transport feel like it lost half the app — and they
 * report back what to run instead.
 */
export function CommandPalette({
  open,
  commands,
  onOpenChange,
  onRun,
}: CommandPaletteProps): React.ReactElement {
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  const filtered = React.useMemo(() => filterCommands(commands, query), [commands, query]);

  React.useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      // Radix focuses the first focusable; ensure it is the filter input.
      window.requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  React.useEffect(() => {
    setActive(0);
  }, [query]);

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
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-label="Command palette">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <DialogDescription className="sr-only">
          Type to filter Quartz slash commands. Enter runs the selected command, Escape closes.
        </DialogDescription>

        <div className="flex items-center gap-2 border-b border-line px-3 py-2">
          <span aria-hidden="true" className="font-mono text-xs text-accent">
            /
          </span>
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Filter commands…"
            aria-label="Filter commands"
            aria-controls="command-list"
            aria-activedescendant={
              filtered[active] ? `command-option-${filtered[active].name}` : undefined
            }
            role="combobox"
            aria-expanded="true"
            spellCheck={false}
            autoComplete="off"
            className="w-full bg-transparent font-mono text-sm text-ink placeholder:text-ink-dim focus-visible:outline-none"
          />
        </div>

        <ul
          id="command-list"
          role="listbox"
          aria-label="Available commands"
          className="max-h-[50vh] overflow-y-auto py-1"
        >
          {filtered.length === 0 ? (
            <li className="px-3 py-4 text-center font-mono text-xs text-ink-dim">
              No command matches “{query}”.
            </li>
          ) : null}
          {filtered.map((command, index) => (
            <li
              key={command.name}
              id={`command-option-${command.name}`}
              role="option"
              aria-selected={index === active}
              className={cn(
                "flex cursor-pointer items-baseline gap-2 px-3 py-1 transition-colors duration-[var(--duration-feedback)]",
                index === active ? "bg-elevated" : "hover:bg-surface",
              )}
              onMouseEnter={() => setActive(index)}
              onClick={() => run(command)}
            >
              <Terminal
                aria-hidden="true"
                className={cn(
                  "size-3 shrink-0 self-center",
                  index === active ? "text-accent" : "text-ink-dim",
                )}
              />
              <span className="shrink-0 font-mono text-xs text-ink">/{command.name}</span>
              {command.usage ? (
                <span className="shrink-0 font-mono text-2xs text-ink-dim">{command.usage}</span>
              ) : null}
              <span className="min-w-0 truncate text-2xs text-ink-muted">
                {command.description}
              </span>
              {index === active ? (
                <CornerDownLeft
                  aria-hidden="true"
                  className="ml-auto size-3 shrink-0 self-center text-ink-dim"
                />
              ) : null}
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
