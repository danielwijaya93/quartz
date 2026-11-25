import * as React from "react";
import { Button, cn } from "@heroui/react";
import { Activity, X } from "lucide-react";
import type { SidebarSession } from "@/lib/sidebar";
import type { ActiveSessionItem } from "@/protocol";
import type { TileBadge } from "@/lib/tiling";

export function ActiveSessionsSection({
  items,
  currentSessionId,
  sessionById,
  renames,
  tileBadges,
  onSelectSession,
  onOpenInNewTile,
  onDismiss,
  onCloseDrawer,
}: {
  items: readonly ActiveSessionItem[];
  currentSessionId: string;
  sessionById: ReadonlyMap<string, SidebarSession>;
  renames: Readonly<Record<string, string>>;
  tileBadges?: ReadonlyMap<string, TileBadge>;
  onSelectSession: (sessionId: string) => void;
  onOpenInNewTile?: (sessionId: string) => void;
  onDismiss?: (sessionId: string) => void;
  onCloseDrawer?: () => void;
}): React.ReactElement {
  return (
    <section aria-label="Active Sessions" className="mb-2 border-b border-separator/70 pb-2">
      <div className="flex h-6 items-center gap-1.5 px-1.5">
        <Activity aria-hidden="true" className="size-3 shrink-0 text-muted" />
        <span className="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wider text-foreground/90">
          Active Sessions
        </span>
        <span className="ml-auto shrink-0 rounded-full bg-surface-secondary px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-muted">
          {items.length}
        </span>
        {onCloseDrawer ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label="Close the session sidebar"
            onPress={onCloseDrawer}
            className="size-6 min-w-6 text-muted hover:text-foreground"
          >
            <X aria-hidden="true" className="size-3.5" />
          </Button>
        ) : null}
      </div>
      {items.length === 0 ? (
        <p className="mt-1 px-2 py-1 text-[11px] text-meta">No active sessions</p>
      ) : (
        <ul aria-label="Active sessions" className="mt-1 space-y-1">
          {items.map((item) => {
            const session = sessionById.get(item.sessionId);
            const title =
              renames[item.sessionId] ?? session?.title ?? item.title ?? "New session";
            const isCurrent = item.sessionId === currentSessionId;
            const isWorking = item.kind === "working";
            const isWaiting = item.kind === "waiting";
            const isNeedsYou = item.kind === "permission" || item.kind === "error";
            const canDismiss = (item.kind === "done" || item.kind === "error") && Boolean(onDismiss);

            const statusLabel = isNeedsYou
              ? "Needs you"
              : isWorking
                ? "Working"
                : isWaiting
                  ? "Waiting"
                  : "Done";
            const dotClass = isNeedsYou
              ? "bg-orange-500"
              : isWorking
                ? "bg-blue-500 motion-safe:animate-pulse"
                : isWaiting
                  ? "bg-amber-500"
                  : "bg-emerald-500";
            const badgeClass = isNeedsYou
              ? "bg-orange-500/15 text-orange-600 dark:text-orange-400"
              : isWorking
                ? "bg-blue-500/15 text-blue-600 dark:text-blue-400"
                : isWaiting
                  ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
                  : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400";
            const rowStyle = isCurrent
              ? "border-accent/40 bg-accent-soft/60"
              : isNeedsYou
                ? "border-orange-500/30 bg-orange-500/5 hover:bg-orange-500/10"
                : isWorking
                  ? "border-blue-500/25 bg-blue-500/5 hover:bg-blue-500/10"
                  : isWaiting
                    ? "border-amber-500/25 bg-amber-500/5 hover:bg-amber-500/10"
                    : "border-emerald-500/25 bg-emerald-500/5 hover:bg-emerald-500/10";

            return (
              <li
                key={item.sessionId}
                className={cn(
                  "group relative flex items-center rounded-md border transition-colors",
                  rowStyle,
                )}
              >
                <button
                  type="button"
                  onClick={(event) => {
                    if ((event.metaKey || event.ctrlKey) && onOpenInNewTile) {
                      event.preventDefault();
                      onOpenInNewTile(item.sessionId);
                      return;
                    }
                    onSelectSession(item.sessionId);
                  }}
                  aria-current={isCurrent ? "true" : undefined}
                  className={cn(
                    "flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus",
                    canDismiss && "pr-7",
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn("size-1.5 shrink-0 rounded-full", dotClass)}
                  />
                  <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">
                    {title}
                  </span>
                  {item.isBot || title.startsWith("[Bot]") ? (
                    <span className="shrink-0 rounded border border-indigo-500/30 bg-indigo-500/10 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-indigo-600 dark:text-indigo-400">
                      Bot
                    </span>
                  ) : null}
                  {(() => {
                    const tb = tileBadges?.get(item.sessionId);
                    return tb ? (
                      <span
                        aria-label={`Open in tile ${tb.number}`}
                        className={cn(
                          "inline-flex size-4 shrink-0 items-center justify-center rounded text-[10px] font-bold tabular-nums",
                          tb.focused
                            ? "bg-accent text-accent-foreground"
                            : "border border-border bg-surface-secondary text-foreground",
                        )}
                      >
                        {tb.number}
                      </span>
                    ) : null;
                  })()}
                  <span
                    className={cn(
                      "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium leading-none",
                      badgeClass,
                    )}
                  >
                    {statusLabel}
                  </span>
                </button>
                {canDismiss ? (
                  <button
                    type="button"
                    onClick={() => onDismiss?.(item.sessionId)}
                    aria-label={`Dismiss ${title}`}
                    title="Dismiss"
                    className="absolute right-1 flex size-5 cursor-pointer items-center justify-center rounded-md text-muted opacity-70 transition-opacity hover:bg-surface-secondary hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
                  >
                    <X aria-hidden="true" className="size-3" />
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
