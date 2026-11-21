import * as React from "react";
import { ConnectionBanner } from "./ConnectionBanner";
import type { ConnectionStatus } from "@/lib/connection";

/**
 * One line above the composer: the server's coarse status label, or a reconnect
 * banner if the socket is unhappy. Non-blocking in both cases.
 */
export function StatusLine({
  label,
  status,
  turnRunning,
}: {
  label: string | null;
  status: ConnectionStatus;
  turnRunning: boolean;
}): React.ReactElement | null {
  if (status !== "connected") {
    return <ConnectionBanner status={status} />;
  }
  if (label === null) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-1 font-mono text-2xs text-ink-muted"
    >
      <span className="size-1.5 animate-pulse rounded-full bg-accent motion-reduce:animate-none" aria-hidden="true" />
      <span>{label}</span>
      {turnRunning ? <span className="text-ink-dim">· running</span> : null}
    </div>
  );
}
