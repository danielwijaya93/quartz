import { Button } from "@heroui/react";
import type { WatcherAction, WatcherState } from "../protocol";

export interface WatcherBannerProps {
  watcher: WatcherState | null | undefined;
  onAction: (action: WatcherAction) => void;
}

export function WatcherBanner({ watcher, onAction }: WatcherBannerProps) {
  if (!watcher) return null;

  const isCountdown = watcher.status === "countdown";
  const isEvaluating = watcher.status === "evaluating";
  if (!isCountdown && !isEvaluating) return null;

  return (
    <div className="mx-auto w-full max-w-3xl px-3 pb-1.5">
      <div
        role="status"
        aria-live="polite"
        className="flex items-center justify-between gap-2 rounded-lg border border-accent/35 bg-accent/10 px-3 py-1.5 text-xs text-foreground"
      >
        <span className="truncate font-medium">
          {isEvaluating
            ? "Turn Watcher checking last 3 messages…"
            : `Turn Watcher continuing in ${watcher.countdownRemaining}s…`}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {isCountdown ? (
            <Button size="sm" variant="primary" onPress={() => onAction("continue_now")}>
              Continue now
            </Button>
          ) : null}
          <Button size="sm" variant="outline" onPress={() => onAction("cancel_countdown")}>
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}
