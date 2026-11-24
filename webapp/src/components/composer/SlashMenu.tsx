import * as React from "react";
import { cn } from "@heroui/react";
import { CornerDownLeft } from "lucide-react";
import { isTerminalOnly } from "../CommandPalette";
import type { Command } from "@/protocol";

export function SlashMenu({
  slashQuery,
  inlineCommands,
  activeCmd,
  cmdListRef,
  onHoverIndex,
  onRunCommand,
}: {
  slashQuery: string | null;
  inlineCommands: readonly Command[];
  activeCmd: number;
  cmdListRef: React.RefObject<HTMLUListElement | null>;
  onHoverIndex: (index: number) => void;
  onRunCommand: (name: string) => void;
}): React.ReactElement {
  return (
    <div
      role="region"
      aria-label="Slash commands"
      className="absolute right-0 bottom-full left-0 z-20 mb-2 overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
    >
      <div className="flex items-center justify-between border-b border-separator px-3 py-2 text-xs text-muted">
        <span className="font-medium text-foreground">Commands</span>
        <span>↑↓ navigate · Tab complete · Enter run · Esc close</span>
      </div>
      {inlineCommands.length === 0 ? (
        <p className="px-3 py-4 text-center text-sm text-muted">
          No command matches “/{slashQuery}”. Press Enter to send as text or Esc to close.
        </p>
      ) : (
        <ul
          ref={cmdListRef}
          id="inline-command-list"
          role="listbox"
          aria-label="Available commands"
          className="max-h-64 space-y-0.5 overflow-y-auto p-1.5"
        >
          {inlineCommands.map((command, index) => {
            const selected = index === activeCmd;
            const terminalOnly = isTerminalOnly(command);
            return (
              <li
                key={command.name}
                id={`inline-command-${command.name}`}
                role="option"
                aria-selected={selected}
                data-selected={selected}
                onMouseEnter={() => onHoverIndex(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  onRunCommand(command.name);
                }}
                className={cn(
                  "flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm",
                  selected
                    ? "bg-accent-soft text-accent-soft-foreground"
                    : "text-foreground hover:bg-surface-secondary",
                  terminalOnly && "opacity-60",
                )}
              >
                <span className="shrink-0 font-mono text-xs font-semibold">
                  /{command.name}
                </span>
                {command.usage ? (
                  <span className="shrink-0 font-mono text-xs text-muted">
                    {command.usage}
                  </span>
                ) : null}
                <span className="min-w-0 flex-1 truncate text-xs text-muted">
                  {command.description}
                </span>
                {terminalOnly ? (
                  <span className="shrink-0 rounded bg-surface-secondary px-1.5 py-0.5 text-[10px] font-medium text-muted">
                    Terminal
                  </span>
                ) : selected ? (
                  <CornerDownLeft aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
