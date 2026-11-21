import * as React from "react";
import { Plus } from "lucide-react";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";
import type { ConnectionStatus } from "@/lib/connection";
import type { SessionMeta } from "@/lib/transcript";

const STATUS_COPY: Record<ConnectionStatus, { label: string; dot: string }> = {
  connected: { label: "connected", dot: "bg-ok" },
  connecting: { label: "connecting", dot: "bg-warn" },
  disconnected: { label: "disconnected", dot: "bg-danger" },
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

export function HeaderBar({
  session,
  status,
  onNewSession,
}: {
  session: SessionMeta;
  status: ConnectionStatus;
  onNewSession: () => void;
}): React.ReactElement {
  const copy = STATUS_COPY[status];
  return (
    <header className="flex shrink-0 items-center gap-3 border-b border-line bg-base px-4 py-1.5">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xs font-semibold tracking-tight text-ink">quartz</span>
        <span className="font-mono text-2xs text-ink-dim">web</span>
      </div>

      <div className="h-3.5 w-px bg-line" aria-hidden="true" />

      <dl className="flex min-w-0 items-center gap-3 font-mono text-2xs">
        <div className="flex items-baseline gap-1.5">
          <dt className="text-ink-dim">session</dt>
          <dd className="text-ink-muted" title={session.sessionId || undefined}>
            {shortSessionId(session.sessionId)}
          </dd>
        </div>
        <div className="flex min-w-0 items-baseline gap-1.5">
          <dt className="text-ink-dim">cwd</dt>
          <dd className="truncate text-ink-muted" title={session.cwd || undefined}>
            {shortCwd(session.cwd)}
          </dd>
        </div>
        <div className="flex items-baseline gap-1.5">
          <dt className="text-ink-dim">model</dt>
          <dd className="text-ink-muted">
            {session.model || "—"}
            {session.effort ? <span className="text-ink-dim"> · {session.effort}</span> : null}
          </dd>
        </div>
      </dl>

      <div className="ml-auto flex items-center gap-2">
        <span
          className="flex items-center gap-1.5 font-mono text-2xs text-ink-muted"
          role="status"
          aria-label={`Connection ${copy.label}`}
        >
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              copy.dot,
              status === "connecting" && "animate-pulse motion-reduce:animate-none",
            )}
          />
          {copy.label}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={onNewSession}
          aria-label="Start a new session"
        >
          <Plus aria-hidden="true" />
          New
        </Button>
      </div>
    </header>
  );
}
