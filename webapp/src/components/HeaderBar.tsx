import * as React from "react";
import { Button, Chip, cn } from "@heroui/react";
import { GitBranch, PanelLeft, Shield } from "lucide-react";
import { RichUiToggle } from "./RichUiToggle";
import { WatcherControl, type WatcherConfigUpdate } from "./WatcherControl";
import type { ConnectionStatus } from "@/lib/connection";
import type { RichUiMode } from "@/lib/richUi";
import type { SessionMeta } from "@/lib/transcript";
import type { WatcherAction, WatcherState } from "@/protocol";

const STATUS_COPY: Record<
  ConnectionStatus,
  { label: string; color: "success" | "warning" | "danger" }
> = {
  connected: { label: "Connected", color: "success" },
  connecting: { label: "Connecting", color: "warning" },
  disconnected: { label: "Disconnected", color: "danger" },
};

/**
 * The app bar.
 */
export const HeaderBar = React.memo(function HeaderBar({
  session,
  status,
  richUiMode,
  onRichUiModeChange,
  onToggleSandbox,
  turnRunning = false,
  sidebarOpen,
  onToggleSidebar,
  fullWidth = false,
  tileControls,
  watcher,
  onConfigureWatcher,
  onWatcherAction,
  onOpenSettings,
}: {
  session: SessionMeta;
  status: ConnectionStatus;
  richUiMode: RichUiMode;
  onRichUiModeChange: (mode: RichUiMode) => void;
  /** Toggles the OS sandbox for the current session. */
  onToggleSandbox?: () => void;
  /** Whether a turn is currently running (disables sandbox toggling). */
  turnRunning?: boolean;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  /** Expand header to full width when multiple tiles are displayed. */
  fullWidth?: boolean;
  /** Optional tiling layout switcher & global worker indicator. */
  tileControls?: React.ReactNode;
  watcher?: WatcherState | null;
  onConfigureWatcher?: (config: WatcherConfigUpdate) => void;
  onWatcherAction?: (action: WatcherAction) => void;
  onOpenSettings?: () => void;
}): React.ReactElement {
  const copy = STATUS_COPY[status];

  const dotClass =
    status === "connected"
      ? "bg-success/80"
      : status === "connecting"
        ? "bg-warning motion-safe:animate-pulse"
        : "bg-danger";

  return (
    <header className="shrink-0 border-b border-separator/70 bg-background/85 backdrop-blur">
      <div
        className={cn(
          "mx-auto flex w-full items-center gap-1.5 px-2.5 py-2 sm:gap-2 sm:px-4",
          !fullWidth && "max-w-3xl sm:px-5",
        )}
      >
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label={sidebarOpen ? "Hide the session sidebar" : "Show the session sidebar"}
          aria-expanded={sidebarOpen}
          onPress={onToggleSidebar}
        >
          <PanelLeft aria-hidden="true" className="size-4" />
        </Button>

        <div className="flex shrink-0 items-center gap-2">
          <span
            aria-hidden="true"
            title={copy.label}
            className={cn("size-2 rounded-full", dotClass)}
          />
          <span className="hidden text-sm font-semibold tracking-tight text-foreground sm:inline">
            Quartz
          </span>
        </div>

        <ul className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden sm:gap-1.5">
          {session.gitBranch ? (
            <li className="flex min-w-0 shrink overflow-hidden sm:shrink-0">
              <Chip
                size="sm"
                variant="soft"
                color="default"
                title={`Git branch: ${session.gitBranch}`}
                aria-label={`Git branch ${session.gitBranch}`}
                className="max-w-full overflow-hidden bg-surface-secondary/50 sm:max-w-36"
              >
                <GitBranch aria-hidden="true" className="mr-1 size-3 shrink-0 text-muted" />
                <span className="truncate font-mono text-[11px] font-medium text-foreground/85">
                  {session.gitBranch}
                </span>
              </Chip>
            </li>
          ) : null}
          <li className="flex min-w-0 shrink-0">
            {(() => {
              const sandboxed = Boolean(session.sandboxed);
              const chip = (
                <Chip
                  size="sm"
                  variant="soft"
                  color={sandboxed ? "success" : "danger"}
                  title={
                    turnRunning
                      ? "Cannot toggle sandbox while a turn is running"
                      : sandboxed
                        ? "Kernel is running inside OS sandbox — click to disable"
                        : "OS sandbox is off — click to enable"
                  }
                  className={cn(
                    sandboxed ? "bg-surface-secondary/50" : "bg-danger/10",
                    onToggleSandbox && !turnRunning && "cursor-pointer transition-colors hover:opacity-85",
                    turnRunning && "cursor-not-allowed opacity-60",
                  )}
                >
                  <Shield
                    aria-hidden="true"
                    className={cn("size-3 shrink-0 sm:mr-1", sandboxed ? "text-success" : "text-danger")}
                  />
                  <span
                    className={cn(
                      "hidden text-[11px] font-medium sm:inline",
                      sandboxed ? "text-foreground/85" : "text-danger line-through",
                    )}
                  >
                    Sandboxed
                  </span>
                </Chip>
              );
              return onToggleSandbox ? (
                <button
                  type="button"
                  disabled={turnRunning}
                  onClick={onToggleSandbox}
                  aria-pressed={sandboxed}
                  aria-label={sandboxed ? "Sandbox enabled — click to disable" : "Sandbox disabled — click to enable"}
                  className="inline-flex shrink-0 items-center rounded-md focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none disabled:cursor-not-allowed"
                >
                  {chip}
                </button>
              ) : (
                chip
              );
            })()}
          </li>
        </ul>

        <div className="ml-auto flex shrink-0 items-center justify-end gap-2">
          <span role="status" aria-label={`Connection ${copy.label}`}>
            {status === "connected" ? (
              <span className="sr-only">{copy.label}</span>
            ) : (
              <Chip size="sm" variant="soft" color={copy.color} className="h-7 text-xs font-medium">
                <span className="flex items-center gap-1.5">
                  <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
                  <span className="hidden md:inline">{copy.label}</span>
                </span>
              </Chip>
            )}
          </span>
          {watcher && onConfigureWatcher && onWatcherAction ? (
            <WatcherControl
              watcher={watcher}
              onConfigure={onConfigureWatcher}
              onAction={onWatcherAction}
              onOpenSettings={onOpenSettings}
            />
          ) : null}
          {tileControls}
          <RichUiToggle mode={richUiMode} onChange={onRichUiModeChange} />
        </div>
      </div>
    </header>
  );
});
