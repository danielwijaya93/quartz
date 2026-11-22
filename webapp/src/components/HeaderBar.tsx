import * as React from "react";
import { Button, Chip, cn } from "@heroui/react";
import { ChevronDown, Folder, PanelLeft, Plus } from "lucide-react";
import { RichUiToggle } from "./RichUiToggle";
import { ThemeToggle } from "./ThemeToggle";
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

/** Short session id: the last 8 chars are the discriminating part. */
export function shortSessionId(sessionId: string): string {
  if (!sessionId) return "—";
  return sessionId.length <= 8 ? sessionId : sessionId.slice(-8);
}

function MetaChip({
  label,
  title,
  className,
  onClick,
  ariaLabel,
}: {
  label: string;
  title?: string;
  className?: string;
  onClick?: () => void;
  ariaLabel?: string;
}): React.ReactElement {
  const inner = (
    <span
      title={title}
      className={cn(
        "block max-w-[15rem] truncate rounded-md px-1.5 py-0.5 font-mono text-[11px] text-meta transition-colors",
        onClick && "cursor-pointer hover:bg-surface-secondary hover:text-foreground",
      )}
    >
      {label}
    </span>
  );

  return (
    <li className={className}>
      {onClick ? (
        <button
          type="button"
          onClick={onClick}
          aria-label={ariaLabel || label}
          title={title}
          className="inline-flex max-w-full cursor-pointer items-center rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
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
 *
 * Session facts read as chips rather than a metrics gutter: the same
 * information, presented as a product header instead of a debug line. Only the
 * theme control and "New" are actions.
 */
export function HeaderBar({
  session,
  status,
  richUiMode,
  onRichUiModeChange,
  onNewSession,
  onChangeFolder,
  onOpenModelPicker,
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
  /** Opens the model picker dialog to switch provider/model. */
  onOpenModelPicker?: () => void;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
}): React.ReactElement {
  const copy = STATUS_COPY[status];
  const model = session.model ? `${session.model}${session.effort ? ` · ${session.effort}` : ""}` : "—";

  return (
    <header className="shrink-0 border-b border-separator bg-background/85 backdrop-blur">
      {/* Two rows at every width, because one row never had room for both. The
          first is what you can do; the second is where you are. A person reads the
          folder far more often than the model name, and on a phone it used to be
          hidden outright. */}
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-1 px-4 py-2 sm:px-5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
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
            <span aria-hidden="true" className="size-2 rounded-full bg-accent ring-2 ring-accent/25" />
            <span className="text-sm font-bold tracking-tight text-foreground">Quartz</span>
          </div>

          {/* One unit that moves to its own line when it does not fit, rather than
              breaking into pieces: four half-width controls around a wordmark is
              harder to scan than two tidy rows. */}
          <div className="ml-auto flex shrink-0 items-center justify-end gap-2">
            <span role="status" aria-label={`Connection ${copy.label}`}>
              <Chip size="sm" variant="soft" color={copy.color} className="h-7 text-xs font-medium">
                <span className="flex items-center gap-1.5">
                  <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
                  <span className="hidden md:inline">{copy.label}</span>
                </span>
              </Chip>
            </span>
            <RichUiToggle mode={richUiMode} onChange={onRichUiModeChange} />
            <ThemeToggle />
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

        {/* Never hidden, unlike the model: a truncated model name is still the model
            name, while the folder has nowhere else to be read. */}
        <ul className="flex min-w-0 items-center gap-1.5 overflow-hidden">
          <MetaChip
            label={shortSessionId(session.sessionId)}
            title={session.sessionId ? `Session ${session.sessionId}` : undefined}
            className="shrink-0"
          />
          <CwdChip cwd={session.cwd} onChangeFolder={onChangeFolder} />
          <MetaChip
            label={model}
            title={model ? `${model} — click to switch model` : "Click to choose a model"}
            className="ml-auto hidden max-w-[14rem] shrink-0 sm:block"
            onClick={onOpenModelPicker}
            ariaLabel={model ? `Switch model (current: ${model})` : "Choose a model"}
          />
        </ul>
      </div>
    </header>
  );
}
