import * as React from "react";
import { cn } from "@heroui/react";
import { Bot, Pencil, Plus, Trash2 } from "lucide-react";
import type { BotConfig, WatcherType } from "@/types/bots";

export interface BotsSectionProps {
  bots: readonly BotConfig[];
  onCreateBot: () => void;
  onEditBot: (bot: BotConfig) => void;
  onToggleBot: (id: string, enabled: boolean) => void;
  onDeleteBot: (id: string) => void;
}

const WATCHER_LABELS: Record<WatcherType, string> = {
  github_poll: "GitHub",
  cron: "Cron",
  webhook: "Webhook",
};

export function BotsSection({
  bots,
  onCreateBot,
  onEditBot,
  onToggleBot,
  onDeleteBot,
}: BotsSectionProps): React.ReactElement {
  return (
    <section aria-label="Bots" className="mb-2 border-b border-separator/70 pb-2">
      <div className="flex h-6 items-center gap-1.5 px-1.5">
        <Bot aria-hidden="true" className="size-3 shrink-0 text-muted" />
        <span className="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wider text-foreground/90">
          Bots
        </span>
        <button
          type="button"
          onClick={onCreateBot}
          className="ml-auto inline-flex cursor-pointer items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium text-accent transition-colors hover:bg-accent-soft/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
        >
          <Plus aria-hidden="true" className="size-2.5" />
          + Create Bot
        </button>
      </div>

      {bots.length === 0 ? (
        <p className="mt-1 px-2 py-1 text-[11px] text-meta">No bots configured</p>
      ) : (
        <ul aria-label="Bots list" className="mt-1 space-y-1">
          {bots.map((bot) => (
            <li
              key={bot.id}
              className="group flex items-center gap-1.5 rounded-md border border-separator/70 bg-surface-secondary/30 px-2 py-1.5 transition-colors hover:bg-surface-secondary/60"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="min-w-0 truncate text-[12px] font-medium text-foreground">
                    {bot.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => onToggleBot(bot.id, !bot.enabled)}
                    title={bot.enabled ? "Pause bot" : "Enable bot"}
                    className={cn(
                      "shrink-0 cursor-pointer rounded px-1.5 py-0.5 text-[10px] font-medium leading-none transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus",
                      bot.enabled
                        ? "bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/25 dark:text-emerald-400"
                        : "bg-surface-secondary text-muted hover:bg-surface-secondary/80",
                    )}
                  >
                    {bot.enabled ? "Enabled" : "Paused"}
                  </button>
                </div>
                <span className="block truncate text-[10px] text-meta">
                  {WATCHER_LABELS[bot.watcher_type]}
                </span>
              </div>

              <div className="flex shrink-0 items-center gap-1">
                <button
                  type="button"
                  role="switch"
                  aria-checked={bot.enabled}
                  aria-label={`Toggle ${bot.name}`}
                  title={bot.enabled ? "Pause bot" : "Enable bot"}
                  onClick={() => onToggleBot(bot.id, !bot.enabled)}
                  className={cn(
                    "relative inline-flex h-4 w-7 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus",
                    bot.enabled ? "bg-emerald-500" : "bg-muted/40",
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "pointer-events-none inline-block size-3 rounded-full bg-white shadow-xs transition-transform",
                      bot.enabled ? "translate-x-3.5" : "translate-x-0.5",
                    )}
                  />
                </button>

                <button
                  type="button"
                  aria-label={`Edit ${bot.name}`}
                  title="Edit bot"
                  onClick={() => onEditBot(bot)}
                  className="flex size-5 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
                >
                  <Pencil aria-hidden="true" className="size-3" />
                </button>

                <button
                  type="button"
                  aria-label={`Delete ${bot.name}`}
                  title="Delete bot"
                  onClick={() => onDeleteBot(bot.id)}
                  className="flex size-5 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-danger-soft hover:text-danger-soft-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
                >
                  <Trash2 aria-hidden="true" className="size-3" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
