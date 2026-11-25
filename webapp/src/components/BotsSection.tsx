import * as React from "react";
import { cn } from "@heroui/react";
import { Bot, ChevronDown, ChevronRight, History, Pencil, Play, Plus, Trash2 } from "lucide-react";
import type { BotConfig, BotRunRecord, WatcherType } from "@/types/bots";

export interface BotsSectionProps {
  bots: readonly BotConfig[];
  runs?: readonly BotRunRecord[];
  activeSessionId?: string;
  togglingIds?: ReadonlySet<string>;
  runningIds?: ReadonlySet<string>;
  toggleError?: string | null;
  onCreateBot: () => void;
  onOpenHistory?: () => void;
  onOpenBotHistory?: (botId: string) => void;
  onOpenSession?: (sessionId: string) => void;
  onEditBot: (bot: BotConfig) => void;
  onToggleBot: (id: string, enabled: boolean) => void;
  onRunBot?: (id: string) => void;
  onDeleteBot: (id: string) => void;
}

const MAX_INLINE_RUNS = 5;

function formatRunTime(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatRunDuration(durationMs: number | null): string {
  if (durationMs === null || durationMs === undefined) return "";
  if (durationMs < 1000) return `${durationMs}ms`;
  const secs = durationMs / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  return `${Math.floor(secs / 60)}m`;
}

function formatRelativeTime(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (delta < 60) return "now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

const WATCHER_LABELS: Record<WatcherType, string> = {
  github_poll: "GitHub",
  cron: "Cron",
  webhook: "Webhook",
};

function formatBotSubtitle(bot: BotConfig): string {
  const base = WATCHER_LABELS[bot.watcher_type];
  if (bot.watcher_type === "cron" && bot.cron?.expression) {
    return `${base} · ${bot.cron.expression}`;
  }
  if (bot.watcher_type === "github_poll" && bot.github?.repo) {
    return `${base} · ${bot.github.repo}`;
  }
  if (bot.watcher_type === "webhook" && bot.webhook?.event_filter) {
    return `${base} · ${bot.webhook.event_filter}`;
  }
  return base;
}

export function BotsSection({
  bots,
  runs = [],
  activeSessionId,
  togglingIds,
  runningIds,
  toggleError,
  onCreateBot,
  onOpenHistory,
  onOpenBotHistory,
  onOpenSession,
  onEditBot,
  onToggleBot,
  onRunBot,
  onDeleteBot,
}: BotsSectionProps): React.ReactElement {
  const [expandedBots, setExpandedBots] = React.useState<Record<string, boolean>>({});

  const runsByBot = React.useMemo(() => {
    const map = new Map<string, BotRunRecord[]>();
    for (const run of runs) {
      const list = map.get(run.bot_id) ?? [];
      list.push(run);
      map.set(run.bot_id, list);
    }
    return map;
  }, [runs]);

  const toggleExpanded = (botId: string) => {
    setExpandedBots((prev) => ({ ...prev, [botId]: !prev[botId] }));
  };
  return (
    <section aria-label="Bots" className="mb-2 border-b border-separator/70 pb-2">
      <div className="flex h-6 items-center gap-1 px-1.5">
        <Bot aria-hidden="true" className="size-3 shrink-0 text-muted" />
        <span className="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wider text-foreground/90">
          Bots
        </span>
        <div className="ml-auto flex items-center gap-1">
          {onOpenHistory ? (
            <button
              type="button"
              onClick={onOpenHistory}
              aria-label="Bots history"
              title="Bots history"
              className="inline-flex cursor-pointer items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
            >
              <History aria-hidden="true" className="size-2.5" />
              History
            </button>
          ) : null}
          <button
            type="button"
            onClick={onCreateBot}
            className="inline-flex cursor-pointer items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium text-accent transition-colors hover:bg-accent-soft/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <Plus aria-hidden="true" className="size-2.5" />
            Create Bot
          </button>
        </div>
      </div>
      {toggleError ? (
        <p role="alert" className="mt-1 px-2 text-[11px] text-danger-soft-foreground">
          {toggleError}
        </p>
      ) : null}

      {bots.length === 0 ? (
        <p className="mt-1 px-2 py-1 text-[11px] text-meta">No bots configured</p>
      ) : (
        <ul aria-label="Bots list" className="mt-1 space-y-1">
          {bots.map((bot) => {
            const isToggling = Boolean(togglingIds?.has(bot.id));
            const isRunning = Boolean(runningIds?.has(bot.id));
            const botRuns = runsByBot.get(bot.id) ?? [];
            const latestRun = botRuns[0];
            const isExpanded = Boolean(expandedBots[bot.id]);
            const baseSubtitle = formatBotSubtitle(bot);
            const subtitle = botRuns.length > 0 ? `${baseSubtitle} · ${botRuns.length} runs` : baseSubtitle;
            const dotClass = !bot.enabled
              ? "bg-muted/50"
              : latestRun?.status === "error"
                ? "bg-danger"
                : latestRun?.status === "waiting_permission"
                  ? "bg-amber-500"
                  : latestRun?.status === "running" || isRunning
                    ? "bg-sky-500 motion-safe:animate-pulse"
                    : "bg-emerald-500";
            return (
              <li
                key={bot.id}
                className="rounded-md border border-separator/70 bg-surface-secondary/30 px-2 py-1.5 transition-colors hover:bg-surface-secondary/50"
              >
                <div className="group flex items-center gap-1.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1">
                      <span
                        aria-hidden="true"
                        className={cn("size-1.5 shrink-0 rounded-full", dotClass)}
                      />
                      {botRuns.length > 0 ? (
                        <button
                          type="button"
                          aria-expanded={isExpanded}
                          aria-label={`Toggle runs for ${bot.name}`}
                          onClick={() => toggleExpanded(bot.id)}
                          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1 rounded text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
                        >
                          {isExpanded ? (
                            <ChevronDown aria-hidden="true" className="size-3 shrink-0 text-muted" />
                          ) : (
                            <ChevronRight aria-hidden="true" className="size-3 shrink-0 text-muted" />
                          )}
                          <span
                            title={bot.name}
                            className="min-w-0 truncate text-[12px] font-medium text-foreground"
                          >
                            {bot.name}
                          </span>
                        </button>
                      ) : (
                        <span
                          title={bot.name}
                          className="min-w-0 truncate pl-0.5 text-[12px] font-medium text-foreground"
                        >
                          {bot.name}
                        </span>
                      )}
                      {latestRun ? (
                        <span className="shrink-0 text-[10px] tabular-nums text-meta">
                          {formatRelativeTime(latestRun.started_at)}
                        </span>
                      ) : null}
                    </div>
                    <span title={subtitle} className="block truncate pl-3 text-[10px] text-meta">
                      {subtitle}
                    </span>
                  </div>

                <div className="flex shrink-0 items-center gap-0.5">
                  {onRunBot ? (
                    <button
                      type="button"
                      aria-label={`Run ${bot.name} now`}
                      title="Run bot now"
                      disabled={isRunning}
                      onClick={() => onRunBot(bot.id)}
                      className="flex size-5 cursor-pointer items-center justify-center rounded text-muted transition-all hover:bg-surface-secondary hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus disabled:cursor-wait disabled:opacity-50 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
                    >
                      <Play aria-hidden="true" className="size-3" />
                    </button>
                  ) : null}

                  <button
                    type="button"
                    aria-label={`Edit ${bot.name}`}
                    title="Edit bot"
                    onClick={() => onEditBot(bot)}
                    className="flex size-5 cursor-pointer items-center justify-center rounded text-muted transition-all hover:bg-surface-secondary hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
                  >
                    <Pencil aria-hidden="true" className="size-3" />
                  </button>

                  <button
                    type="button"
                    aria-label={`Delete ${bot.name}`}
                    title="Delete bot"
                    onClick={() => onDeleteBot(bot.id)}
                    className="flex size-5 cursor-pointer items-center justify-center rounded text-muted transition-all hover:bg-danger-soft hover:text-danger-soft-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
                  >
                    <Trash2 aria-hidden="true" className="size-3" />
                  </button>

                  <button
                    type="button"
                    role="switch"
                    aria-checked={bot.enabled}
                    aria-label={`Toggle ${bot.name}`}
                    title={bot.enabled ? "Pause bot" : "Enable bot"}
                    disabled={isToggling}
                    onClick={() => onToggleBot(bot.id, !bot.enabled)}
                    className={cn(
                      "relative ml-0.5 inline-flex h-4 w-7 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus disabled:cursor-wait disabled:opacity-60",
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
                </div>
                </div>

                {isExpanded && botRuns.length > 0 ? (
                  <div className="mt-1.5 ml-2 space-y-0.5 border-l border-separator pl-2">
                    {botRuns.slice(0, MAX_INLINE_RUNS).map((run) => {
                      const isActive = Boolean(activeSessionId && run.session_id === activeSessionId);
                      const statusGlyph =
                        run.status === "ok"
                          ? "✓"
                          : run.status === "waiting_permission"
                            ? "⚠"
                            : run.status === "error"
                              ? "✕"
                              : "●";
                      const glyphClass =
                        run.status === "ok"
                          ? "text-emerald-600 dark:text-emerald-400"
                          : run.status === "waiting_permission"
                            ? "text-amber-600 dark:text-amber-400"
                            : run.status === "error"
                              ? "text-danger-soft-foreground"
                              : "text-sky-500";
                      const dur = formatRunDuration(run.duration_ms);
                      const timeText = formatRunTime(run.started_at);
                      return (
                        <button
                          key={run.run_id}
                          type="button"
                          disabled={!run.session_id || !onOpenSession}
                          aria-label={`Open run ${timeText}`}
                          onClick={() => {
                            if (run.session_id && onOpenSession) onOpenSession(run.session_id);
                          }}
                          className={cn(
                            "flex w-full cursor-pointer items-center justify-between rounded px-1.5 py-1 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus disabled:cursor-default disabled:opacity-60",
                            isActive
                              ? "bg-accent-soft/70 font-semibold text-accent"
                              : "text-foreground/85 hover:bg-surface-secondary",
                          )}
                        >
                          <span className="flex items-center gap-1.5">
                            <span aria-hidden="true" className={cn("font-bold", glyphClass)}>
                              {statusGlyph}
                            </span>
                            <span className="tabular-nums">{timeText}</span>
                          </span>
                          {dur ? <span className="tabular-nums text-[10px] text-meta">{dur}</span> : null}
                        </button>
                      );
                    })}
                    {(onOpenBotHistory || onOpenHistory) ? (
                      <button
                        type="button"
                        onClick={() => {
                          if (onOpenBotHistory) onOpenBotHistory(bot.id);
                          else onOpenHistory?.();
                        }}
                        className="block w-full cursor-pointer px-1.5 pt-0.5 text-left text-[10.5px] font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
                      >
                        View all {botRuns.length} runs →
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
