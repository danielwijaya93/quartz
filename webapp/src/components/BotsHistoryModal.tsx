import * as React from "react";
import { cn } from "@heroui/react";
import { ExternalLink, History, RefreshCw, RotateCcw, X } from "lucide-react";
import { fetchBotsHistory } from "@/lib/botsApi";
import { QuartzSelect } from "./QuartzSelect";
import type { BotConfig, BotRunRecord, DeletedBotRecord } from "@/types/bots";

export interface BotsHistoryModalProps {
  open: boolean;
  bots: readonly BotConfig[];
  initialBotId?: string;
  onClose: () => void;
  onOpenSession: (sessionId: string) => void;
  onRestoreBot?: (id: string) => Promise<unknown>;
}

const STATUS_STYLES: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  running: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  waiting_permission: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  error: "bg-danger-soft text-danger-soft-foreground",
  cancelled: "bg-surface-secondary text-muted",
};

const STATUS_LABELS: Record<string, string> = {
  ok: "Success",
  running: "Running",
  waiting_permission: "Needs approval",
  error: "Error",
  cancelled: "Cancelled",
};

function formatTime(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null || durationMs === undefined) return "—";
  if (durationMs < 1000) return `${durationMs}ms`;
  const secs = durationMs / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const mins = Math.floor(secs / 60);
  const rem = Math.round(secs % 60);
  return `${mins}m ${rem}s`;
}

export function BotsHistoryModal({
  open,
  bots,
  initialBotId,
  onClose,
  onOpenSession,
  onRestoreBot,
}: BotsHistoryModalProps): React.ReactElement {
  const headingId = React.useId();
  const dialogRef = React.useRef<HTMLDialogElement | null>(null);

  const [tab, setTab] = React.useState<"runs" | "deleted">("runs");
  const [botFilter, setBotFilter] = React.useState<string>(initialBotId ?? "");

  React.useEffect(() => {
    if (open) {
      setTab("runs");
      setBotFilter(initialBotId ?? "");
    }
  }, [open, initialBotId]);
  const [statusFilter, setStatusFilter] = React.useState<string>("");
  const [runs, setRuns] = React.useState<BotRunRecord[]>([]);
  const [deletedBots, setDeletedBots] = React.useState<DeletedBotRecord[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [restoringId, setRestoringId] = React.useState<string | null>(null);

  const loadHistory = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchBotsHistory({
        botId: botFilter || undefined,
        status: statusFilter || undefined,
        limit: 100,
      });
      setRuns(data.runs);
      setDeletedBots(data.deletedBots);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load bots history.");
    } finally {
      setLoading(false);
    }
  }, [botFilter, statusFilter]);

  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      void loadHistory();
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [open, loadHistory]);

  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const handleCancel = (event: Event) => {
      event.preventDefault();
      onClose();
    };
    dialog.addEventListener("cancel", handleCancel);
    return () => dialog.removeEventListener("cancel", handleCancel);
  }, [onClose]);

  const handleRestore = async (id: string) => {
    if (!onRestoreBot) return;
    setRestoringId(id);
    setError(null);
    try {
      await onRestoreBot(id);
      await loadHistory();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to restore bot.");
    } finally {
      setRestoringId(null);
    }
  };

  const botOptions = React.useMemo(() => {
    const map = new Map<string, string>();
    for (const b of bots) map.set(b.id, b.name);
    for (const d of deletedBots) {
      if (!map.has(d.bot.id)) map.set(d.bot.id, `${d.bot.name} (deleted)`);
    }
    for (const r of runs) {
      if (!map.has(r.bot_id)) map.set(r.bot_id, r.bot_name);
    }
    return Array.from(map.entries());
  }, [bots, deletedBots, runs]);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      className="m-auto w-[min(42rem,calc(100vw-2rem))] rounded-2xl border border-border bg-surface p-6 text-foreground shadow-2xl backdrop:bg-black/50 backdrop:backdrop-blur-xs"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent">
            <History aria-hidden="true" className="size-4" />
          </div>
          <h2 id={headingId} className="text-base font-semibold">
            Bots History
          </h2>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => void loadHistory()}
            disabled={loading}
            aria-label="Refresh history"
            title="Refresh"
            className="flex size-7 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus disabled:opacity-50"
          >
            <RefreshCw aria-hidden="true" className={cn("size-3.5", loading && "animate-spin")} />
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex size-7 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 border-b border-separator pb-2">
        <button
          type="button"
          onClick={() => setTab("runs")}
          className={cn(
            "cursor-pointer rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            tab === "runs"
              ? "bg-accent text-accent-foreground"
              : "text-muted hover:bg-surface-secondary hover:text-foreground",
          )}
        >
          Run History ({runs.length})
        </button>
        <button
          type="button"
          onClick={() => setTab("deleted")}
          className={cn(
            "cursor-pointer rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            tab === "deleted"
              ? "bg-accent text-accent-foreground"
              : "text-muted hover:bg-surface-secondary hover:text-foreground",
          )}
        >
          Deleted Bots ({deletedBots.length})
        </button>
      </div>

      {error ? (
        <p
          role="alert"
          className="mt-3 rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger-soft-foreground"
        >
          {error}
        </p>
      ) : null}

      {tab === "runs" ? (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <QuartzSelect
              aria-label="Filter by bot"
              value={botFilter}
              onChange={setBotFilter}
              className="w-40"
              triggerClassName="h-8"
              options={[
                { value: "", label: "All bots" },
                ...botOptions.map(([id, name]) => ({ value: id, label: name })),
              ]}
            />
            <QuartzSelect
              aria-label="Filter by status"
              value={statusFilter}
              onChange={setStatusFilter}
              className="w-40"
              triggerClassName="h-8"
              options={[
                { value: "", label: "All statuses" },
                { value: "ok", label: "Success" },
                { value: "running", label: "Running" },
                { value: "waiting_permission", label: "Needs approval" },
                { value: "error", label: "Error" },
                { value: "cancelled", label: "Cancelled" },
              ]}
            />
          </div>

          <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
            {runs.length === 0 ? (
              <p className="py-6 text-center text-xs text-meta">
                {loading ? "Loading run history…" : "No bot runs recorded yet."}
              </p>
            ) : (
              runs.map((run) => (
                <div
                  key={run.run_id}
                  className="rounded-lg border border-separator/80 bg-surface-secondary/30 p-3 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-foreground">{run.bot_name}</span>
                      <span
                        className={cn(
                          "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
                          STATUS_STYLES[run.status] ?? "bg-surface-secondary text-muted",
                        )}
                      >
                        {STATUS_LABELS[run.status] ?? run.status}
                      </span>
                    </div>
                    <span className="shrink-0 text-[11px] text-meta">
                      {formatDuration(run.duration_ms)}
                    </span>
                  </div>

                  <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-meta">
                    <span className="truncate">
                      {run.source} · {run.event_key}
                    </span>
                    <span>{formatTime(run.started_at)}</span>
                  </div>

                  {run.error ? (
                    <p className="mt-1.5 rounded bg-danger-soft/60 px-2 py-1 text-[11px] text-danger-soft-foreground">
                      {run.error}
                    </p>
                  ) : null}

                  {run.session_id ? (
                    <div className="mt-2 flex justify-end">
                      <button
                        type="button"
                        onClick={() => {
                          onOpenSession(run.session_id!);
                          onClose();
                        }}
                        className="inline-flex cursor-pointer items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent-soft/60"
                      >
                        <ExternalLink aria-hidden="true" className="size-3" />
                        Open session
                      </button>
                    </div>
                  ) : null}
                </div>
              ))
            )}
          </div>
        </div>
      ) : (
        <div className="mt-3 max-h-80 space-y-2 overflow-y-auto pr-1">
          {deletedBots.length === 0 ? (
            <p className="py-6 text-center text-xs text-meta">
              {loading ? "Loading deleted bots…" : "No deleted bots."}
            </p>
          ) : (
            deletedBots.map((entry) => (
              <div
                key={entry.bot.id}
                className="flex items-center justify-between gap-3 rounded-lg border border-separator/80 bg-surface-secondary/30 p-3 text-xs"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-foreground">{entry.bot.name}</div>
                  <div className="mt-0.5 truncate text-[11px] text-meta">
                    {entry.bot.watcher_type} · Deleted {formatTime(entry.deleted_at)}
                  </div>
                </div>
                {onRestoreBot ? (
                  <button
                    type="button"
                    disabled={restoringId === entry.bot.id}
                    onClick={() => void handleRestore(entry.bot.id)}
                    className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary disabled:cursor-wait disabled:opacity-60"
                  >
                    <RotateCcw aria-hidden="true" className="size-3" />
                    Restore
                  </button>
                ) : null}
              </div>
            ))
          )}
        </div>
      )}
    </dialog>
  );
}
