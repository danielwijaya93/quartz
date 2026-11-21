import * as React from "react";
import { Button, Chip, Separator } from "@heroui/react";
import { Plus } from "lucide-react";
import { RichUiToggle } from "./RichUiToggle";
import { ThemeToggle } from "./ThemeToggle";
import type { ConnectionStatus } from "@/lib/connection";
import type { RichUiMode } from "@/lib/richUi";
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

/** Collapse a home-relative cwd so long paths do not eat the header. */
export function shortCwd(cwd: string): string {
  if (!cwd) return "—";
  const parts = cwd.split("/").filter(Boolean);
  if (parts.length <= 3) return cwd;
  return `…/${parts.slice(-2).join("/")}`;
}

function MetaChip({
  label,
  title,
  className,
}: {
  label: string;
  title?: string;
  className?: string;
}): React.ReactElement {
  return (
    <li className={className}>
      <Chip size="sm" variant="soft" color="default" title={title}>
        <span className="block max-w-[15rem] truncate">{label}</span>
      </Chip>
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
}: {
  session: SessionMeta;
  status: ConnectionStatus;
  richUiMode: RichUiMode;
  onRichUiModeChange: (mode: RichUiMode) => void;
  onNewSession: () => void;
}): React.ReactElement {
  const copy = STATUS_COPY[status];
  const model = session.model ? `${session.model}${session.effort ? ` · ${session.effort}` : ""}` : "—";

  return (
    <header className="shrink-0 border-b border-separator bg-background/85 backdrop-blur">
      <div className="mx-auto flex w-full max-w-3xl items-center gap-3 px-4 py-2.5 sm:px-5">
        <div className="flex shrink-0 items-center gap-2">
          <span aria-hidden="true" className="size-2.5 rounded-full bg-accent" />
          <span className="text-sm font-semibold tracking-tight text-foreground">Quartz</span>
        </div>

        <Separator orientation="vertical" className="hidden h-4 sm:block" />

        <ul className="hidden min-w-0 flex-1 items-center gap-1.5 overflow-hidden sm:flex">
          <MetaChip
            label={shortSessionId(session.sessionId)}
            title={session.sessionId ? `Session ${session.sessionId}` : undefined}
            className="shrink-0"
          />
          <MetaChip
            label={shortCwd(session.cwd)}
            title={session.cwd || undefined}
            className="min-w-0 overflow-hidden"
          />
          <MetaChip label={model} title={model} className="shrink-0" />
        </ul>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          <span role="status" aria-label={`Connection ${copy.label}`}>
            <Chip size="sm" variant="soft" color={copy.color}>
              <span className="flex items-center gap-1.5">
                <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
                <span className="hidden md:inline">{copy.label}</span>
              </span>
            </Chip>
          </span>
          <RichUiToggle mode={richUiMode} onChange={onRichUiModeChange} />
          <ThemeToggle />
          <Button size="sm" variant="outline" onPress={onNewSession} aria-label="Start a new session">
            <Plus aria-hidden="true" className="size-3.5" />
            <span className="hidden md:inline">New</span>
          </Button>
        </div>
      </div>
    </header>
  );
}