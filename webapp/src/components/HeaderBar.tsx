import * as React from "react";
import { Button, Chip, cn } from "@heroui/react";
import { ChevronDown, Folder, GitBranch, PanelLeft, Plus } from "lucide-react";
import { RichUiToggle } from "./RichUiToggle";
import type { ConnectionStatus } from "@/lib/connection";
import type { RichUiMode } from "@/lib/richUi";
import { directoryLabel } from "@/lib/sidebar";
import type { SessionMeta } from "@/lib/transcript";

const STATUS_COPY: Record<
  ConnectionStatus,
  { label: string; color: "success" | "warning" | "danger" }
> = {
  connected: { label: "Connected", color: "success" },
  connecting: { label: "Connecting", color: "warning" },
  disconnected: { label: "Disconnected", color: "danger" },
};

/**
 * Where the session is running: the folder's own name, never a stub.
 */
function CwdChip({
  cwd,
  onChangeFolder,
}: {
  cwd: string;
  onChangeFolder?: () => void;
}): React.ReactElement {
  const inner = (
    <Chip
      size="sm"
      variant="soft"
      color="default"
      title={cwd || undefined}
      className={cn(
        "max-w-full min-w-0 overflow-hidden bg-surface-secondary/70",
        onChangeFolder && "cursor-pointer transition-colors hover:bg-surface-secondary",
      )}
    >
      <Folder aria-hidden="true" className="mr-1 size-3 shrink-0 text-muted" />
      <span className="sr-only">
        {cwd ? `Working directory ${cwd}` : "Working directory not set"}
      </span>
      <span aria-hidden="true" dir="rtl" className="block truncate text-left font-medium">
        {cwd ? directoryLabel(cwd) : "—"}
      </span>
      {onChangeFolder ? (
        <ChevronDown aria-hidden="true" className="ml-0.5 size-3 shrink-0 text-muted" />
      ) : null}
    </Chip>
  );

  return (
    <li className="min-w-0 flex-1">
      {onChangeFolder ? (
        <button
          type="button"
          onClick={onChangeFolder}
          aria-label={`Switch folder (current: ${cwd || "not set"})`}
          title={cwd ? `${cwd} — click to switch folder` : "Click to choose a folder"}
          className="inline-flex max-w-full min-w-0 cursor-pointer items-center rounded-md focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          {inner}
        </button>
      ) : (
        inner
      )}
    </li>
  );
}

/**
 * The app bar.
 */
export function HeaderBar({
  session,
  status,
  richUiMode,
  onRichUiModeChange,
  onNewSession,
  onChangeFolder,
  sidebarOpen,
  onToggleSidebar,
}: {
  session: SessionMeta;
  status: ConnectionStatus;
  richUiMode: RichUiMode;
  onRichUiModeChange: (mode: RichUiMode) => void;
  /** Starts a new session in the current folder (or opens the picker if unset). */
  onNewSession: () => void;
  /** Opens the folder picker to start a session in another folder. */
  onChangeFolder?: () => void;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
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
      <div className="mx-auto flex w-full max-w-3xl items-center gap-2 px-4 py-2 sm:px-5">
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
          <span className="text-sm font-semibold tracking-tight text-foreground">Quartz</span>
        </div>

        <span aria-hidden="true" className="text-xs text-muted/60">
          /
        </span>

        <ul className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden">
          <CwdChip cwd={session.cwd} onChangeFolder={onChangeFolder} />
          {session.gitBranch ? (
            <li className="hidden min-w-0 shrink-0 sm:flex">
              <Chip
                size="sm"
                variant="soft"
                color="default"
                title={`Git branch: ${session.gitBranch}`}
                aria-label={`Git branch ${session.gitBranch}`}
                className="max-w-36 overflow-hidden bg-surface-secondary/50"
              >
                <GitBranch aria-hidden="true" className="mr-1 size-3 shrink-0 text-muted" />
                <span className="truncate font-mono text-[11px] font-medium text-foreground/85">
                  {session.gitBranch}
                </span>
              </Chip>
            </li>
          ) : null}
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
          <RichUiToggle mode={richUiMode} onChange={onRichUiModeChange} />
          <Button
            size="sm"
            variant="outline"
            onPress={onNewSession}
            aria-label="Start a new session"
            className="h-7 gap-1 border-border/80 bg-surface-secondary/40 font-medium hover:bg-surface-secondary hover:text-foreground"
          >
            <Plus aria-hidden="true" className="size-3.5" />
            <span className="hidden md:inline">New</span>
          </Button>
        </div>
      </div>
    </header>
  );
}
