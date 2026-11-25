import * as React from "react";
import { cn } from "@heroui/react";
import { ArrowLeft, Bot, Pencil, RotateCw } from "lucide-react";
import type { BotConfig, BotRunRecord } from "@/types/bots";

export interface BotRunBannerProps {
  run: BotRunRecord;
  bot?: BotConfig;
  isRunning?: boolean;
  onOpenHistory: (botId: string) => void;
  onEditBot?: (bot: BotConfig) => void;
  onRerun?: (botId: string) => void;
}

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  running: "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  waiting_permission: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  error: "bg-danger-soft text-danger-soft-foreground",
  cancelled: "bg-surface-secondary text-muted",
};

const STATUS_LABEL: Record<string, string> = {
  ok: "✓ Success",
  running: "● Running",
  waiting_permission: "⚠ Needs Approval",
  error: "✕ Error",
  cancelled: "Cancelled",
};

function formatDuration(durationMs: number | null): string {
  if (durationMs === null || durationMs === undefined) return "";
  if (durationMs < 1000) return `${durationMs}ms`;
  const secs = durationMs / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const mins = Math.floor(secs / 60);
  const rem = Math.round(secs % 60);
  return `${mins}m ${rem}s`;
}

export function BotRunBanner({
  run,
  bot,
  isRunning = false,
  onOpenHistory,
  onEditBot,
  onRerun,
}: BotRunBannerProps): React.ReactElement {
  const statusLabel = STATUS_LABEL[run.status] ?? run.status;
  const duration = formatDuration(run.duration_ms);
  const startedText = run.started_at
    ? new Date(run.started_at * 1000).toLocaleString()
    : "";

  return (
    <div
      role="region"
      aria-label="Bot run details"
      className="mx-auto mt-3 flex w-full max-w-3xl flex-wrap items-center justify-between gap-3 rounded-xl border border-separator bg-surface-secondary/40 px-4 py-2.5"
    >
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1 rounded border border-indigo-500/30 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-indigo-600 dark:text-indigo-400">
            <Bot aria-hidden="true" className="size-3" />
            Bot Run
          </span>
          <span className="truncate text-[13px] font-semibold text-foreground">
            {run.bot_name || bot?.name || run.bot_id}
          </span>
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[11px] font-semibold",
              STATUS_STYLE[run.status] ?? "bg-surface-secondary text-muted",
            )}
          >
            {statusLabel}
            {duration ? ` · ${duration}` : ""}
          </span>
        </div>
        <p className="mt-0.5 truncate text-[11.5px] text-muted">
          Triggered by {run.source || "bot"}
          {startedText ? ` · ${startedText}` : ""}
          {bot?.cwd ? ` · ${bot.cwd}` : ""}
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <button
          type="button"
          onClick={() => onOpenHistory(run.bot_id)}
          className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-separator bg-surface px-2.5 py-1 text-[11.5px] font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
        >
          <ArrowLeft aria-hidden="true" className="size-3" />
          All Runs
        </button>
        {bot && onEditBot ? (
          <button
            type="button"
            onClick={() => onEditBot(bot)}
            className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-separator bg-surface px-2.5 py-1 text-[11.5px] font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <Pencil aria-hidden="true" className="size-3" />
            Edit Bot
          </button>
        ) : null}
        {onRerun ? (
          <button
            type="button"
            disabled={isRunning}
            onClick={() => onRerun(run.bot_id)}
            className="inline-flex cursor-pointer items-center gap-1 rounded-lg bg-foreground px-2.5 py-1 text-[11.5px] font-semibold text-background transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus disabled:cursor-wait disabled:opacity-50"
          >
            <RotateCw aria-hidden="true" className={cn("size-3", isRunning && "animate-spin")} />
            Re-run Now
          </button>
        ) : null}
      </div>
    </div>
  );
}
